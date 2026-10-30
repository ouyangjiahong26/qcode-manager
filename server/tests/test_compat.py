"""兼容层转换器单测：anthropic / responses 两个翻译层的纯函数部分。

只测纯转换逻辑（请求/响应/SSE 事件/usage 口径/凭据编解码），不 import
gateway / accountsvc / upstreamsvc 等运行期依赖——两个模块对这些依赖全部
延迟导入，本文件才能在网关装配前独立运行。
"""
from __future__ import annotations

import json
import unittest

from server.routers import anthropic, responses


def parse_events(chunks: list[bytes]) -> list[tuple[str, dict]]:
    """把 `_StreamTranslator` 吐出的事件字节解析成 (event, data) 列表。"""
    out: list[tuple[str, dict]] = []
    for chunk in chunks:
        text = chunk.decode('utf-8')
        # 每个事件是 `event: 名字\ndata: JSON\n\n`，逐个切出来
        for block in text.split('\n\n'):
            block = block.strip()
            if not block:
                continue
            name = ''
            payload: dict = {}
            for line in block.split('\n'):
                if line.startswith('event: '):
                    name = line[len('event: '):]
                elif line.startswith('data: '):
                    payload = json.loads(line[len('data: '):])
            out.append((name, payload))
    return out


class AnthropicEstimateTokensTest(unittest.TestCase):
    """字符粗估：CJK 1 字 1 token、其余 4 字符 1 token（宁可高估）。"""

    def test_empty(self):
        self.assertEqual(anthropic._estimate_tokens(''), 0)

    def test_cjk(self):
        self.assertEqual(anthropic._estimate_tokens('你好世界'), 4)

    def test_ascii(self):
        self.assertEqual(anthropic._estimate_tokens('abcd'), 1)
        self.assertEqual(anthropic._estimate_tokens('abcde'), 2)  # (5+3)//4

    def test_mixed(self):
        self.assertEqual(anthropic._estimate_tokens('你好abcd'), 3)


class AnthropicTextOfTest(unittest.TestCase):
    def test_string_passthrough(self):
        self.assertEqual(anthropic._text_of('hi'), 'hi')

    def test_blocks(self):
        content = [
            {'type': 'text', 'text': 'a'},
            {'type': 'image', 'source': {'type': 'url', 'url': 'http://x'}},
            {'type': 'text', 'text': 'b'},
        ]
        self.assertEqual(anthropic._text_of(content), 'a\nb')

    def test_image_url_shapes(self):
        self.assertEqual(
            anthropic._image_url({'type': 'base64', 'media_type': 'image/png', 'data': 'QUJD'}),
            'data:image/png;base64,QUJD')
        self.assertEqual(anthropic._image_url({'type': 'url', 'url': 'http://x'}), 'http://x')
        self.assertIsNone(anthropic._image_url({'type': 'other'}))
        self.assertIsNone(anthropic._image_url('bad'))


class AnthropicRequestConversionTest(unittest.TestCase):
    def test_system_and_plain_text(self):
        body = {
            'model': 'm1', 'max_tokens': 100, 'system': 'be brief',
            'messages': [{'role': 'user', 'content': 'hi'}],
        }
        out = anthropic.to_openai_request(body)
        self.assertEqual(out['messages'][0], {'role': 'system', 'content': 'be brief'})
        self.assertEqual(out['messages'][1], {'role': 'user', 'content': 'hi'})
        self.assertEqual(out['max_tokens'], 100)

    def test_tool_use_and_result(self):
        body = {
            'model': 'm1', 'max_tokens': 50,
            'messages': [
                {'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'calling'},
                    {'type': 'tool_use', 'id': 'tu1', 'name': 'get_weather',
                     'input': {'city': '北京'}},
                ]},
                {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'tu1',
                     'content': 'sunny'},
                    {'type': 'text', 'text': 'thanks'},
                ]},
            ],
        }
        out = anthropic.to_openai_request(body)
        msgs = out['messages']
        # assistant：tool_calls + 文本
        self.assertEqual(msgs[0]['role'], 'assistant')
        self.assertEqual(msgs[0]['content'], 'calling')
        call = msgs[0]['tool_calls'][0]
        self.assertEqual(call['id'], 'tu1')
        self.assertEqual(call['function']['name'], 'get_weather')
        self.assertEqual(json.loads(call['function']['arguments']), {'city': '北京'})
        # 工具结果拆成独立 role:tool 消息，且先于 user 文本
        self.assertEqual(msgs[1], {'role': 'tool', 'tool_call_id': 'tu1', 'content': 'sunny'})
        self.assertEqual(msgs[2], {'role': 'user', 'content': 'thanks'})

    def test_tools_and_choice(self):
        body = {
            'model': 'm1', 'max_tokens': 10,
            'messages': [{'role': 'user', 'content': 'x'}],
            'tools': [{'name': 't', 'description': 'd',
                       'input_schema': {'type': 'object', 'properties': {}}}],
            'tool_choice': {'type': 'any'},
        }
        out = anthropic.to_openai_request(body)
        self.assertEqual(out['tools'][0]['function']['name'], 't')
        self.assertEqual(out['tool_choice'], 'required')

    def test_reasoning_effort_sources(self):
        base = {'model': 'm', 'max_tokens': 1,
                'messages': [{'role': 'user', 'content': 'x'}]}
        self.assertEqual(
            anthropic.to_openai_request({**base, 'output_config': {'effort': 'high'}})
            .get('reasoning_effort'), 'high')
        self.assertEqual(
            anthropic.to_openai_request({**base, 'thinking': {'type': 'enabled',
                                                              'budget_tokens': 8000}})
            .get('reasoning_effort'), 'medium')
        # ≥64k 表达「不限思考」，映射到最强档 max（而不是次高的 xhigh）
        self.assertEqual(
            anthropic.to_openai_request({**base, 'thinking': {'type': 'enabled',
                                                              'budget_tokens': 100000}})
            .get('reasoning_effort'), 'max')
        self.assertIsNone(anthropic.to_openai_request(base).get('reasoning_effort'))

    def test_thinking_enabled(self):
        self.assertTrue(anthropic._thinking_enabled({'thinking': {'type': 'enabled'}}))
        self.assertTrue(anthropic._thinking_enabled({'thinking': {'type': 'adaptive'}}))
        self.assertFalse(anthropic._thinking_enabled({'thinking': {'type': 'off'}}))
        self.assertFalse(anthropic._thinking_enabled({}))


class AnthropicResponseConversionTest(unittest.TestCase):
    def _openai(self, **over):
        data = {
            'choices': [{'message': {'role': 'assistant', 'content': 'answer'},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 7},
        }
        data.update(over)
        return data

    def test_basic_shape(self):
        out = anthropic.to_anthropic_response(self._openai(), 'claude-x')
        self.assertEqual(out['type'], 'message')
        self.assertEqual(out['model'], 'claude-x')
        self.assertEqual(out['content'], [{'type': 'text', 'text': 'answer'}])
        self.assertEqual(out['stop_reason'], 'end_turn')
        self.assertEqual(out['usage']['input_tokens'], 100)
        self.assertEqual(out['usage']['output_tokens'], 7)

    def test_usage_excludes_cache(self):
        # Anthropic 口径：input 不含缓存（OpenAI 的 prompt_tokens 含），否则客户端相加会重复计
        out = anthropic.to_anthropic_response(self._openai(usage={
            'prompt_tokens': 100, 'completion_tokens': 7,
            'prompt_cache_hit_tokens': 20, 'prompt_cache_write_tokens': 10,
        }), 'm')
        self.assertEqual(out['usage']['input_tokens'], 70)
        self.assertEqual(out['usage']['cache_read_input_tokens'], 20)
        self.assertEqual(out['usage']['cache_creation_input_tokens'], 10)

    def test_tool_calls_and_stop_reason(self):
        data = self._openai()
        data['choices'][0]['message']['content'] = None
        data['choices'][0]['message']['tool_calls'] = [{
            'id': 'c1', 'function': {'name': 'f', 'arguments': '{"a": 1}'},
        }]
        data['choices'][0]['finish_reason'] = 'tool_calls'
        out = anthropic.to_anthropic_response(data, 'm')
        self.assertEqual(out['stop_reason'], 'tool_use')
        self.assertEqual(out['content'][0]['type'], 'tool_use')
        self.assertEqual(out['content'][0]['input'], {'a': 1})

    def test_thinking_block_signature_roundtrip(self):
        data = self._openai()
        data['choices'][0]['message']['reasoning_content'] = 'let me think'
        out = anthropic.to_anthropic_response(data, 'm', thinking=True)
        block = out['content'][0]
        self.assertEqual(block['type'], 'thinking')
        self.assertEqual(block['thinking'], 'let me think')
        # signature 必须能解回推理原文（客户端回传后用于满足上游校验）
        self.assertEqual(responses._decode_credential(block['signature']), 'let me think')


class AnthropicStreamTranslatorTest(unittest.TestCase):
    def _run(self, frames, **kw):
        t = anthropic._StreamTranslator('m', **kw)
        events: list[bytes] = []
        for f in frames:
            events += t.feed(f)
        events += t.finish(None, force=True)
        return parse_events(events)

    def test_text_stream_sequence(self):
        frames = [
            {'choices': [{'delta': {'content': 'He'}}]},
            {'choices': [{'delta': {'content': 'llo'}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 3, 'completion_tokens': 2}},
        ]
        names = [n for n, _ in self._run(frames)]
        # 结构：message_start → content_block_start → 两次 delta → stop → message_delta → message_stop
        self.assertEqual(names[0], 'message_start')
        self.assertEqual(names[1], 'content_block_start')
        self.assertEqual(names[2:4], ['content_block_delta', 'content_block_delta'])
        self.assertIn('content_block_stop', names)
        self.assertEqual(names[-2], 'message_delta')
        self.assertEqual(names[-1], 'message_stop')

    def test_text_deltas_and_indexes(self):
        events = self._run([{'choices': [{'delta': {'content': 'Hi'}}]},
                            {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}])
        deltas = [p for n, p in events if n == 'content_block_delta']
        self.assertEqual(deltas[0]['delta'], {'type': 'text_delta', 'text': 'Hi'})
        starts = [p for n, p in events if n == 'content_block_start']
        self.assertEqual(starts[0]['content_block']['type'], 'text')
        # delta 的 index 必须与块的 index 一致（客户端按 index 建槽）
        self.assertEqual(deltas[0]['index'], starts[0]['index'])

    def test_thinking_then_text(self):
        frames = [
            {'choices': [{'delta': {'reasoning_content': 'ponder'}}]},
            {'choices': [{'delta': {'content': 'A'}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
        ]
        events = self._run(frames, thinking=True)
        names = [n for n, _ in events]
        # 思考块先开（index 0），正文是第二个块（index 1）
        starts = [p for n, p in events if n == 'content_block_start']
        self.assertEqual([s['content_block']['type'] for s in starts], ['thinking', 'text'])
        self.assertEqual([s['index'] for s in starts], [0, 1])
        # 关思考块前先发 signature_delta（签名是客户端回传推理的唯一凭据）
        sig_i = names.index('content_block_delta')  # 第一个 delta 即 thinking_delta
        deltas = [p for n, p in events if n == 'content_block_delta']
        kinds = [d['delta']['type'] for d in deltas]
        self.assertEqual(kinds[0], 'thinking_delta')
        self.assertIn('signature_delta', kinds)
        # signature_delta 必须出现在 thinking 块的 content_block_stop 之前
        think_stop_i = next(i for i, p in enumerate(events)
                            if events[i][0] == 'content_block_stop' and events[i][1]['index'] == 0)
        sig_event_i = next(i for i, (n, p) in enumerate(events)
                           if n == 'content_block_delta'
                           and p['delta']['type'] == 'signature_delta')
        self.assertLess(sig_event_i, think_stop_i)
        # 末帧 usage 进 message_delta（input 含真实值）
        md = [p for n, p in events if n == 'message_delta'][0]
        self.assertEqual(md['delta']['stop_reason'], 'end_turn')

    def test_tool_stream(self):
        frames = [
            {'choices': [{'delta': {'tool_calls': [{
                'id': 'c1', 'function': {'name': 'f', 'arguments': '{"a"'}}]}}]},
            {'choices': [{'delta': {'tool_calls': [
                {'function': {'arguments': ': 1}'}}]}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]},
        ]
        events = self._run(frames)
        deltas = [p for n, p in events if n == 'content_block_delta']
        tool_deltas = [d for d in deltas if d['delta']['type'] == 'input_json_delta']
        # 参数分片原样透传，由客户端拼接
        self.assertEqual(''.join(d['delta']['partial_json'] for d in tool_deltas), '{"a": 1}')
        md = [p for n, p in events if n == 'message_delta'][0]
        self.assertEqual(md['delta']['stop_reason'], 'tool_use')

    def test_usage_and_cache_in_message_delta(self):
        frames = [{'choices': [{'delta': {'content': 'x'}}]},
                  {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 50, 'completion_tokens': 5,
                             'prompt_cache_hit_tokens': 10}}]
        events = self._run(frames)
        md = [p for n, p in events if n == 'message_delta'][0]
        self.assertEqual(md['usage']['input_tokens'], 40)   # 50 - 10 命中
        self.assertEqual(md['usage']['cache_read_input_tokens'], 10)
        self.assertEqual(md['usage']['output_tokens'], 5)


class CredentialCodecTest(unittest.TestCase):
    def test_roundtrip(self):
        token = responses._encode_credential('推理原文')
        self.assertTrue(token.startswith('qcm1:'))
        self.assertEqual(responses._decode_credential(token), '推理原文')

    def test_foreign_token_rejected(self):
        # 客户端回传真·OpenAI 的加密串：解不开也不报错，交调用方回落
        self.assertIsNone(responses._decode_credential('enc-foreign-blob'))
        self.assertIsNone(responses._decode_credential(None))
        self.assertIsNone(responses._decode_credential('qcm1:!!!not-base64!!!'))


class ResponsesRequestConversionTest(unittest.TestCase):
    def test_string_input_and_instructions(self):
        body = {'model': 'm', 'stream': True, 'instructions': 'sys',
                'input': 'hello'}
        out = responses.to_chat_request(body)
        self.assertTrue(out['stream'])  # stream 必须转告上游
        self.assertEqual(out['messages'][0], {'role': 'system', 'content': 'sys'})
        self.assertEqual(out['messages'][1], {'role': 'user', 'content': 'hello'})

    def test_function_call_round(self):
        body = {
            'model': 'm',
            'input': [
                {'type': 'function_call', 'call_id': 'call_1', 'name': 'f',
                 'arguments': '{"x": 1}'},
                {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'ok'},
            ],
        }
        out = responses.to_chat_request(body)
        msgs = out['messages']
        self.assertEqual(msgs[0]['role'], 'assistant')
        self.assertEqual(msgs[0]['tool_calls'][0]['id'], 'call_1')
        self.assertEqual(json.loads(msgs[0]['tool_calls'][0]['function']['arguments']),
                         {'x': 1})
        self.assertEqual(msgs[1], {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'ok'})

    def test_orphan_output_becomes_user(self):
        # Codex 形状：只有 function_call_output、没有对应调用 → 降级 user，避免上游 11148
        body = {'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': '', 'name': 'task',
             'output': 'do something'},
        ]}
        msgs = responses.to_chat_request(body)['messages']
        self.assertEqual(msgs[0]['role'], 'user')
        self.assertIn('[tool output: task]', msgs[0]['content'])
        self.assertIn('do something', msgs[0]['content'])

    def test_reasoning_attached_to_assistant(self):
        body = {'model': 'm', 'input': [
            {'type': 'reasoning', 'content': 'why'},
            {'type': 'message', 'role': 'assistant', 'content': 'answer'},
        ]}
        msgs = responses.to_chat_request(body)['messages']
        # 两个 reasoning 字段都写：请求侧校验读 reasoning（社区对照实验）
        self.assertEqual(msgs[0].get('reasoning'), 'why')
        self.assertEqual(msgs[0].get('reasoning_content'), 'why')
        self.assertEqual(msgs[0]['content'], 'answer')

    def test_sampling_and_effort(self):
        body = {'model': 'm', 'input': 'x', 'max_output_tokens': 64,
                'temperature': 0.2, 'reasoning': {'effort': 'high'},
                'prompt_cache_key': 'sess-1'}
        out = responses.to_chat_request(body)
        self.assertEqual(out['max_tokens'], 64)
        self.assertEqual(out['temperature'], 0.2)
        self.assertEqual(out['reasoning_effort'], 'high')
        self.assertEqual(out['prompt_cache_key'], 'sess-1')
        # 上游不认识的专有字段不透传
        self.assertNotIn('store', out)
        self.assertNotIn('reasoning', out)


class ResponsesObjectConversionTest(unittest.TestCase):
    def test_text_answer(self):
        data = {'choices': [{'message': {'role': 'assistant', 'content': 'hi'},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}
        out = responses.to_responses_object(data, 'm', 'resp_1')
        self.assertEqual(out['id'], 'resp_1')
        self.assertEqual(out['status'], 'completed')
        self.assertEqual(out['output_text'], 'hi')
        self.assertEqual(out['output'][0]['type'], 'message')
        self.assertEqual(out['output'][0]['content'], [
            {'type': 'output_text', 'text': 'hi', 'annotations': []}])
        self.assertEqual(out['usage']['total_tokens'], 12)

    def test_usage_keeps_cache_inside_input(self):
        # OpenAI 语义：input_tokens 含缓存，缓存数放 details（与 anthropic 方向相反）
        out = responses.to_responses_object({}, 'm', 'r')  # 先只测 _usage_object
        u = responses._usage_object({'prompt_tokens': 100, 'completion_tokens': 5,
                                     'prompt_tokens_details': {'cached_tokens': 30}})
        self.assertEqual(u['input_tokens'], 100)
        self.assertEqual(u['input_tokens_details']['cached_tokens'], 30)
        self.assertEqual(u['total_tokens'], 105)

    def test_tool_call_item(self):
        data = {'choices': [{'message': {'tool_calls': [
            {'id': 'call_9', 'function': {'name': 'f', 'arguments': '{"a":1}'}}]},
            'finish_reason': 'tool_calls'}]}
        out = responses.to_responses_object(data, 'm', 'r')
        item = out['output'][0]
        self.assertEqual(item['type'], 'function_call')
        self.assertEqual(item['call_id'], 'call_9')
        self.assertEqual(item['arguments'], '{"a":1}')

    def test_length_truncation(self):
        data = {'choices': [{'message': {'content': 'partial'}, 'finish_reason': 'length'}]}
        out = responses.to_responses_object(data, 'm', 'r')
        self.assertEqual(out['status'], 'incomplete')
        self.assertEqual(out['incomplete_details'], {'reason': 'max_output_tokens'})

    def test_reasoning_item_carries_credential(self):
        data = {'choices': [{'message': {'reasoning_content': 'why', 'content': 'a'},
                             'finish_reason': 'stop'}]}
        out = responses.to_responses_object(data, 'm', 'r')
        rs = out['output'][0]
        self.assertEqual(rs['type'], 'reasoning')
        self.assertEqual(responses._decode_credential(rs['encrypted_content']), 'why')


class ResponsesStreamTranslatorTest(unittest.TestCase):
    def _run(self, frames, **kw):
        t = responses._StreamTranslator('m', 'resp_t', **kw)
        events: list[bytes] = []
        for f in frames:
            events += t.feed(f)
        events += t.finish(getattr(t, 'pending_finish', None), force=True)
        return parse_events(events)

    def test_text_stream(self):
        frames = [
            {'choices': [{'delta': {'content': 'He'}}]},
            {'choices': [{'delta': {'content': 'llo'}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 4, 'completion_tokens': 2}},
        ]
        events = self._run(frames)
        names = [n for n, _ in events]
        self.assertEqual(names[0], 'response.created')
        self.assertIn('response.output_item.added', names)
        self.assertIn('response.content_part.added', names)
        self.assertEqual(names[-1], 'response.completed')
        # 文本增量事件携带原文分片
        deltas = [p for n, p in events if n == 'response.output_text.delta']
        self.assertEqual([d['delta'] for d in deltas], ['He', 'llo'])
        # 增量与 output_item.added 的 output_index 一致（不一致会被客户端静默丢弃）
        added = [p for n, p in events
                 if n == 'response.output_item.added' and p['item']['type'] == 'message']
        for d in deltas:
            self.assertEqual(d['output_index'], added[0]['output_index'])
        # 终止事件带完整 usage
        done = events[-1][1]
        self.assertEqual(done['response']['usage']['total_tokens'], 6)
        self.assertEqual(done['response']['output'][0]['type'], 'message')

    def test_reasoning_then_text(self):
        frames = [
            {'choices': [{'delta': {'reasoning_content': 'think'}}]},
            {'choices': [{'delta': {'content': 'A'}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
        ]
        events = self._run(frames)
        names = [n for n, _ in events]
        self.assertIn('response.reasoning_summary_text.delta', names)
        added_types = [p['item']['type'] for n, p in events
                       if n == 'response.output_item.added']
        self.assertEqual(added_types, ['reasoning', 'message'])
        # reasoning 的 output_item.done 必须带 encrypted_content 凭据
        rs_done = [p for n, p in events
                   if n == 'response.output_item.done' and p['item']['type'] == 'reasoning']
        self.assertEqual(responses._decode_credential(
            rs_done[0]['item']['encrypted_content']), 'think')

    def test_tool_stream(self):
        frames = [
            {'choices': [{'delta': {'tool_calls': [{
                'id': 'call_1', 'function': {'name': 'f', 'arguments': '{"a"'}}]}}]},
            {'choices': [{'delta': {'tool_calls': [
                {'function': {'arguments': ': 2}'}}]}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]},
        ]
        events = self._run(frames)
        names = [n for n, _ in events]
        self.assertIn('response.function_call_arguments.delta', names)
        self.assertIn('response.function_call_arguments.done', names)
        done = [p for n, p in events
                if n == 'response.output_item.done' and p['item']['type'] == 'function_call']
        self.assertEqual(done[0]['item']['arguments'], '{"a": 2}')
        self.assertEqual(events[-1][0], 'response.completed')

    def test_usage_only_frame_after_finish(self):
        # include_usage 的独立末帧：finish 之后到达，只能更新 usage、不能再吐内容事件
        frames = [
            {'choices': [{'delta': {'content': 'x'}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'prompt_tokens': 9, 'completion_tokens': 1}},
        ]
        events = self._run(frames, defer_finish=True)
        # 终止事件仍在最后（usage 帧不产生新事件、也不破坏顺序）
        self.assertEqual(events[-1][0], 'response.completed')


if __name__ == '__main__':
    unittest.main()

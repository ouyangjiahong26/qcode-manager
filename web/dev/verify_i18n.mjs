#!/usr/bin/env node
/**
 * i18n 键集校验：四个翻译语言（zh-TW/en/ja/ko）的每个域文件，
 * 叶子键集合必须与 zh-CN 完全一致，且 {placeholder} 占位符逐键相同。
 * CI / 本地均可跑：node dev/verify_i18n.mjs（退出码非 0 = 有缺口）。
 */
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const LOCALES_DIR = path.join(
  path.dirname(fileURLToPath(import.meta.url)), '..', 'lib', 'i18n', 'locales',
);
const LOCALES = ['zh-TW', 'en', 'ja', 'ko'];

/** 展开成「完整叶子路径 -> 值」；复数对象（{one,other}）继续下钻成叶子。 */
function flat(obj, prefix = '') {
  const out = {};
  for (const [key, value] of Object.entries(obj)) {
    const full = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      Object.assign(out, flat(value, full));
    } else {
      out[full] = value;
    }
  }
  return out;
}

/** 提取译文里的 {placeholder} 名单（去重、排序，便于比较） */
function placeholders(value) {
  return [...new Set(String(value).match(/\{[^}]+\}/g) ?? [])].sort();
}

const zhDir = path.join(LOCALES_DIR, 'zh-CN');
const domains = fs.readdirSync(zhDir).filter((f) => f.endsWith('.json')).sort();

let failures = 0;
for (const file of domains) {
  const zhLeaves = flat(JSON.parse(fs.readFileSync(path.join(zhDir, file), 'utf8')));
  const zhKeys = Object.keys(zhLeaves).sort();
  for (const locale of LOCALES) {
    const localeFile = path.join(LOCALES_DIR, locale, file);
    if (!fs.existsSync(localeFile)) {
      console.error(`✗ ${locale}/${file}: 文件缺失`);
      failures++;
      continue;
    }
    const leaves = flat(JSON.parse(fs.readFileSync(localeFile, 'utf8')));
    const keys = Object.keys(leaves);
    const missing = zhKeys.filter((k) => !(k in leaves));
    const extra = keys.filter((k) => !(k in zhLeaves));
    if (missing.length) {
      console.error(`✗ ${locale}/${file}: 缺 ${missing.length} 键: ${missing.slice(0, 5).join(', ')}${missing.length > 5 ? ' …' : ''}`);
      failures++;
    }
    if (extra.length) {
      console.error(`✗ ${locale}/${file}: 多 ${extra.length} 键: ${extra.slice(0, 5).join(', ')}${extra.length > 5 ? ' …' : ''}`);
      failures++;
    }
    for (const key of zhKeys) {
      if (!(key in leaves)) continue;
      if (placeholders(zhLeaves[key]).join('|') !== placeholders(leaves[key]).join('|')) {
        console.error(`✗ ${locale}/${file}: ${key} 占位符不一致 (zh: ${placeholders(zhLeaves[key]).join(',')} / ${locale}: ${placeholders(leaves[key]).join(',') || '无'})`);
        failures++;
      }
    }
  }
}

if (failures) {
  console.error(`\n共 ${failures} 处键集/占位符问题`);
  process.exit(1);
}
console.log(`i18n OK: ${domains.length} 个域 × ${LOCALES.length} 个语言键集与 zh-CN 完全一致`);

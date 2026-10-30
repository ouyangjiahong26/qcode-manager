'use client';

import {useState, type ReactNode} from 'react';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {Input} from '@/components/ui/input';
import {errText} from '@/lib/api/http';
import {settingsApi} from '@/lib/api/settings';
import {BASE_PATH} from '@/lib/base-path';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';

/**
 * 重置某个用户的密码。
 *
 * 长度判据（至少 12 位）与后端 security.password_problem 对齐。前端拦一道只是
 * 省掉一次必然失败的往返，**不是安全边界**——所以锁的是确认键而不是输入框。
 */
const MIN_PASSWORD = 12;

export function ResetPasswordDialog({
  username,
  trigger,
}: {
  username: string;
  trigger: ReactNode;
}) {
  const t = useT();
  const [pwd, setPwd] = useState('');

  return (
    <ConfirmDialog
      title={t('settings.resetPasswordTitle', {name: username})}
      description={t('settings.resetPasswordDesc')}
      content={
        <Input
          type="password"
          autoComplete="new-password"
          autoFocus
          value={pwd}
          onChange={(e) => setPwd(e.target.value)}
          placeholder={t('settings.newPasswordPlaceholder')}
          aria-label={t('settings.newPasswordPlaceholder')}
        />
      }
      confirmText={t('settings.resetPassword')}
      confirmDisabled={pwd.length < MIN_PASSWORD}
      // 关掉就清空：弹窗实例随列表行复用，留着上一次的输入会串到下一个用户
      onOpenChange={(open) => {
        if (!open) setPwd('');
      }}
      onConfirm={async () => {
        try {
          const r = await settingsApi.updateUser(username, {password: pwd});
          // 改密码会吊销该用户既有会话；改的是自己时当前登录态也随之失效，
          // 必须明确引导重新登录，否则下一次请求 401 会被直接踢去登录页。
          if (r?.relogin_required) {
            notify.ok(t('settings.passwordUpdated'), t('settings.passwordRelogin'));
            window.setTimeout(() => {
              window.location.href = `${BASE_PATH}/login`;
            }, 1800);
            return;
          }
          notify.ok(t('settings.passwordUpdated'), t('settings.passwordOthersRevoked'));
        } catch (e) {
          notify.err(errText(e));
        }
      }}
      trigger={trigger}
    />
  );
}

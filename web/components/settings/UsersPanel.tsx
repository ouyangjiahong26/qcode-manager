'use client';

/**
 * 管理端用户面板：新增用户、改角色、重置密码、删除。
 *
 * 数据流遵循设置页契约：useAsyncAll 单字段（users）取数，逐项成败三态渲染；
 * 写操作成功后 reload。改角色 / 改密码都会吊销目标用户会话（后端 sv 递增），
 * 界面上如实提示。
 */
import {useState} from 'react';
import {Plus, Trash2, Users} from 'lucide-react';

import {errText} from '@/lib/api/http';
import {settingsApi} from '@/lib/api/settings';
import {useAsyncAll} from '@/lib/use-async-data';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {UserItem} from '@/lib/types/settings';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {ResetPasswordDialog} from './ResetPasswordDialog';

export function UsersPanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const {values, errors, isInitialLoading, reload} = useAsyncAll(
    {users: () => settingsApi.users()},
    [],
  );
  const users: UserItem[] = values.users ?? [];
  const usersFailed = 'users' in errors;

  const [newUser, setNewUser] = useState({username: '', password: '', role: 'viewer'});
  const [busy, setBusy] = useState(false);

  if (isInitialLoading) {
    return (
      <div className="rounded-[20px] bg-muted p-4">
        <SkeletonBar className="mb-4 h-3.5 w-24" />
        <div className="space-y-2">
          {Array.from({length: 3}, (_, i) => (
            <SkeletonBar key={i} className="h-10 w-full rounded-xl" />
          ))}
        </div>
      </div>
    );
  }
  if (usersFailed) {
    return <LoadError variant="page" onRetry={reload} />;
  }

  return (
    <div className="space-y-4">
      {isAdmin && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-1 text-sm font-medium">{t('settings.usersNew')}</div>
          <div className="mb-3 text-[11px] text-muted-foreground">
            {t('settings.usersNewDesc')}
          </div>
          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.username')}
              </Label>
              <Input
                value={newUser.username}
                onChange={(e) => setNewUser({...newUser, username: e.target.value})}
                className="bg-background"
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.password')}
              </Label>
              <Input
                type="password"
                value={newUser.password}
                onChange={(e) => setNewUser({...newUser, password: e.target.value})}
                className="bg-background"
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.usersRole')}
              </Label>
              <Select
                value={newUser.role}
                onValueChange={(v) => setNewUser({...newUser, role: v})}
              >
                <SelectTrigger className="bg-background" aria-label={t('settings.usersRole')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="admin">{t('settings.roleAdmin')}</SelectItem>
                  <SelectItem value="viewer">{t('settings.roleViewer')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <Button
              className="rounded-full"
              disabled={busy}
              onClick={async () => {
                if (!newUser.username.trim() || !newUser.password) {
                  notify.err(t('settings.usersRequired'));
                  return;
                }
                setBusy(true);
                try {
                  await settingsApi.addUser(newUser);
                  notify.ok(t('settings.usersCreated'));
                  setNewUser({username: '', password: '', role: 'viewer'});
                  await reload();
                } catch (e) {
                  notify.err(errText(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              <Plus />
              {t('settings.btnCreate')}
            </Button>
          </div>
        </div>
      )}

      <div className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">
                {t('settings.username')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('settings.usersRole')}
              </TableHead>
              {isAdmin && (
                <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">
                  {t('settings.colActions')}
                </TableHead>
              )}
            </TableRow>
          </TableHeader>
          <TableBody>
            {users.map((u) => (
              <TableRow key={u.username} className="border-b border-border/40">
                <TableCell className="pl-4 text-sm font-medium">{u.username}</TableCell>
                <TableCell>
                  {isAdmin ? (
                    // 行内改角色：切 Select 即提交（低风险操作，不弹确认）；
                    // 「降掉最后一个管理员」这类非法操作由后端 400 拦下并弹原因
                    <Select
                      value={u.role}
                      onValueChange={async (v) => {
                        if (v === u.role) return;
                        try {
                          await settingsApi.updateUser(u.username, {role: v});
                          notify.ok(t('settings.roleUpdated'));
                          await reload();
                        } catch (e) {
                          notify.err(errText(e));
                        }
                      }}
                    >
                      <SelectTrigger
                        className="h-7 w-32 bg-background text-xs"
                        aria-label={t('settings.usersRole')}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="admin">{t('settings.roleAdminShort')}</SelectItem>
                        <SelectItem value="viewer">{t('settings.roleViewerShort')}</SelectItem>
                      </SelectContent>
                    </Select>
                  ) : (
                    <Badge variant="secondary" className="rounded-full">
                      {u.role === 'admin'
                        ? t('settings.roleAdminShort')
                        : t('settings.roleViewerShort')}
                    </Badge>
                  )}
                </TableCell>
                {isAdmin && (
                  <TableCell className="pr-4 text-right">
                    <div className="flex justify-end gap-1">
                      <ResetPasswordDialog
                        username={u.username}
                        trigger={
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 rounded-full text-xs"
                          >
                            {t('settings.resetPassword')}
                          </Button>
                        }
                      />
                      <ConfirmDialog
                        title={t('settings.deleteUserTitle', {name: u.username})}
                        description={t('settings.deleteUserDesc')}
                        confirmText={t('settings.btnDelete')}
                        destructive
                        onConfirm={async () => {
                          try {
                            await settingsApi.removeUser(u.username);
                            notify.ok(t('settings.btnDeleted'));
                            await reload();
                          } catch (e) {
                            notify.err(errText(e));
                          }
                        }}
                        trigger={
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 rounded-md text-red-500 hover:text-red-600"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        }
                      />
                    </div>
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {/* 取不到用户列表时**不能**说「暂无管理用户」——那等于断言系统里一个账号
            都没有。两者含义完全相反，失败时如实说「未取到」并给重试。 */}
        {!users.length &&
          (usersFailed ? (
            <LoadError message={t('state.loadFailed')} onRetry={reload} />
          ) : (
            <EmptyState
              icon={Users}
              title={t('settings.usersEmpty')}
              description={t('settings.usersEmptyDesc')}
              className="flex flex-col items-center justify-center py-12 text-center"
            />
          ))}
      </div>
    </div>
  );
}

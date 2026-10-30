'use client';

/**
 * 「关于」卡片：静态说明（版本、构建时间、仓库地址）。
 *
 * 版本号走 /api/system/versions（与一键更新同一来源，更新后能直接看到变化）；
 * 构建时间由 next.config.ts 在构建期注入 NEXT_PUBLIC_BUILD_TIME。
 */
import {useEffect, useState} from 'react';
import {Info} from 'lucide-react';

import {systemApi} from '@/lib/api/system';
import {useT} from '@/lib/i18n/provider';
import {RichText} from '@/lib/i18n/rich-text';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';

const BUILD_TIME = process.env.NEXT_PUBLIC_BUILD_TIME || '';
const REPO_URL = 'https://github.com/fengyinxia/qcode-manager';

export function AboutCard() {
  const t = useT();
  const [version, setVersion] = useState<string | null>(null);

  useEffect(() => {
    systemApi.versions().then((r) => setVersion(r.manager)).catch(() => setVersion(null));
  }, []);

  return (
    <div className="rounded-[20px] bg-muted p-5 text-xs leading-6 text-muted-foreground">
      <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
        <Info className="h-4 w-4" />
        QCode Manager
      </div>
      <div className="mb-3 flex flex-wrap gap-x-6 gap-y-1">
        <span>
          {t('settings.aboutVersion')}：
          {version === null ? (
            <SkeletonBar className="inline-block h-3 w-16 align-middle" />
          ) : (
            <span className="font-mono text-foreground">{version}</span>
          )}
        </span>
        {BUILD_TIME && (
          <span>
            {t('settings.aboutBuildTime')}：
            <span className="font-mono text-foreground">{BUILD_TIME}</span>
          </span>
        )}
        <span>
          {t('settings.aboutRepo')}：
          <a
            className="text-foreground underline-offset-2 hover:underline"
            href={REPO_URL}
            target="_blank"
            rel="noreferrer"
          >
            fengyinxia/qcode-manager
          </a>
        </span>
      </div>
      <p>{t('settings.aboutDesc')}</p>
      <p className="mt-3">
        <RichText text={t('settings.aboutUsage')} />
      </p>
    </div>
  );
}

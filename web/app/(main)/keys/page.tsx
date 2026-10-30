'use client';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';

/** 占位页：路由与布局底座由本片负责，页面内容由对应业务片替换。 */
export default function KeysPage() {
  return (
    <div className="space-y-4">
      <PageHeader title="密钥" description="网关密钥的分发与额度" />
      <PageSectionTabs />
    </div>
  );
}

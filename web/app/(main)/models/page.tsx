'use client';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';

/** 占位页：路由与布局底座由本片负责，页面内容由对应业务片替换。 */
export default function ModelsPage() {
  return (
    <div className="space-y-4">
      <PageHeader title="模型" description="上游模型目录" />
      <PageSectionTabs />
    </div>
  );
}

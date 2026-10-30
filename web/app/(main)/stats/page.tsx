'use client';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';

/** 占位页：路由与布局底座由本片负责，页面内容由对应业务片替换。 */
export default function StatsPage() {
  return (
    <div className="space-y-4">
      <PageHeader title="统计" description="用量统计与趋势" />
      <PageSectionTabs />
    </div>
  );
}

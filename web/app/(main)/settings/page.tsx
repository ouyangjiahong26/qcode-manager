'use client';

import {useEffect} from 'react';
import {useRouter} from 'next/navigation';
import {DEFAULT_SETTINGS_TAB} from '@/lib/settings-tabs';

/** 只做跳转：内容全部在 layout 里，默认 Tab 由 settings-tabs 统一定义。 */
export default function SettingsIndexPage() {
  const router = useRouter();
  useEffect(() => {
    router.replace(`/settings/${DEFAULT_SETTINGS_TAB}`);
  }, [router]);
  return null;
}

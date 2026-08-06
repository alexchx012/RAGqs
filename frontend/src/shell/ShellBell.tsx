/*
 * 壳层铃铛组合（fe-shared-shell）：自管面板 open 状态，点击条目跳转后关面板。
 * 主页右上角（共用基座 §3.1）与抽屉页头右侧（§5.1）各挂一个，共用同一 NotificationsStore。
 */

import { useState } from 'react';
import { useNavigate } from 'react-router';
import { NotificationBell } from '../notifications/Bell';
import type { NotificationsStore } from '../notifications/store';

export function ShellBell({ store }: { store: NotificationsStore }) {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  return (
    <NotificationBell
      open={open}
      onOpenChange={setOpen}
      onNavigate={(path) => {
        setOpen(false);
        navigate(path);
      }}
      store={store}
    />
  );
}

export function formatDateTime(value?: string | null): string {
  if (!value) return '时间未记录';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(parsed);
}

export function formatFullDateTime(value?: string | null): string {
  if (!value) return '未记录';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(parsed);
}

export function compactId(value?: string | null): string {
  if (!value) return '—';
  return value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

export function targetKindLabel(value?: string | null): string {
  const labels: Record<string, string> = {
    windows: 'Windows 设备',
    desktop: '桌面设备',
    android: 'Android 设备',
    emulator: 'Android 模拟器',
    browser: '浏览器',
  };
  return value ? labels[value.toLowerCase()] || value : '通用设备';
}

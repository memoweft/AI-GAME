import {
  AlertTriangle,
  CheckCircle2,
  CircleHelp,
  Clock3,
  CirclePause,
  CirclePlay,
  XCircle,
} from 'lucide-react';
import type { StatusIcon, StatusMeta } from '../status';

const icons: Record<StatusIcon, typeof CheckCircle2> = {
  check: CheckCircle2,
  clock: Clock3,
  play: CirclePlay,
  pause: CirclePause,
  warning: AlertTriangle,
  error: XCircle,
  help: CircleHelp,
};

interface StatusBadgeProps {
  meta: StatusMeta;
  compact?: boolean;
}

export function StatusBadge({ meta, compact = false }: StatusBadgeProps) {
  const Icon = icons[meta.icon];
  return (
    <span className={`status-badge status-${meta.tone}`} title={meta.detail}>
      <Icon aria-hidden="true" size={compact ? 13 : 14} strokeWidth={2.2} />
      <span>{meta.label}</span>
    </span>
  );
}

import type { Blocker, Run, RuntimeCapability, RuntimeInfo, Target, Workflow } from './types';

/** Keep temporary backend compatibility at the API boundary, not in page markup. */
export function runBlockers(run: Run): Blocker[] {
  const blockers = Array.isArray(run.blockers)
    ? run.blockers
    : !run.blocker
      ? []
      : typeof run.blocker === 'string'
    ? [{ code: 'legacy_blocker', message: run.blocker }]
    : [run.blocker];
  return blockers.map((blocker) => blocker.code === 'executor_not_configured'
    ? {
      code: 'workflow_executor_not_connected',
      message: '传统任务队列尚未接入；请在“对话执行”中操作设备。',
    }
    : blocker);
}

export function modelCapability(runtime?: RuntimeInfo | null): RuntimeCapability | undefined {
  return runtime?.capabilities?.find((item) => ['model_runtime', 'model'].includes(item.id));
}

export function executorCapability(runtime?: RuntimeInfo | null): RuntimeCapability | undefined {
  return runtime?.capabilities?.find((item) => ['gui_executor', 'executor'].includes(item.id));
}

export function capabilityRole(id: string): 'model' | 'executor' | 'planner' | 'adb' | 'other' {
  if (['model_runtime', 'model'].includes(id)) return 'model';
  if (['gui_executor', 'executor'].includes(id)) return 'executor';
  if (['cloud_planner', 'planner'].includes(id)) return 'planner';
  if (id === 'adb') return 'adb';
  return 'other';
}

export function workflowStatus(workflow: Workflow): string {
  if (workflow.status) return workflow.status;
  if (workflow.enabled === false) return 'disabled';
  if (workflow.integration_status) {
    const legacy = workflow.integration_status.toLowerCase();
    if (['available', 'ready', 'integrated'].includes(legacy)) return 'available';
    if (['external', 'owned_application'].includes(legacy)) return 'external';
    if (['future', 'planned', 'placeholder'].includes(legacy)) return 'future';
    if (['disabled', 'paused'].includes(legacy)) return 'disabled';
    return 'not_configured';
  }
  return workflow.enabled === true ? 'available' : 'not_configured';
}

export function targetKind(target: Target): string {
  return (target.kind || target.target_kind || target.platform || '').toLowerCase();
}

export function workflowTargetKinds(workflow: Workflow): string[] {
  const values = workflow.target_kinds?.length
    ? workflow.target_kinds
    : workflow.target_kind
      ? [workflow.target_kind]
      : [];
  return values.map((value) => value.toLowerCase());
}

export function targetMatchesWorkflow(target: Target, workflow?: Workflow): boolean {
  if (!workflow) return false;
  const allowed = workflowTargetKinds(workflow);
  if (allowed.length === 0) return true;
  const kind = targetKind(target);
  if (!kind) return false;
  return allowed.some((candidate) => {
    if (candidate === kind) return true;
    if (candidate === 'desktop' && ['windows', 'macos', 'linux'].includes(kind)) return true;
    if (candidate === 'android' && ['emulator', 'android_emulator'].includes(kind)) return true;
    return false;
  });
}

import { useState } from 'react';
import { AlertCircle, HelpCircle, Send } from 'lucide-react';
import { ApiError, newIdempotencyKey } from '../api';
import type { NeedUserFact } from '../types';

interface QuestionCardProps {
  need: NeedUserFact;
  goalTitle?: string | null;
  onAnswer: (value: string, idempotencyKey: string) => Promise<void>;
}

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : '回答没有保存，请重试。';
}

export function QuestionCard({ need, goalTitle, onAnswer }: QuestionCardProps) {
  const [value, setValue] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<{ value: string; idempotencyKey: string } | null>(null);

  const submit = async () => {
    const normalized = value.trim();
    if (!normalized || submitting) return;
    const request = pending?.value === normalized
      ? pending
      : { value: normalized, idempotencyKey: newIdempotencyKey() };
    setSubmitting(true);
    setError(null);
    try {
      await onAnswer(request.value, request.idempotencyKey);
      setPending(null);
    } catch (caught) {
      setPending(request);
      setError(errorText(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <article className="fact-question-card">
      <div className="fact-question-icon"><HelpCircle size={20} /></div>
      <div className="fact-question-content">
        <span className="eyebrow">需要你补充一个事实</span>
        <h4>{need.question}</h4>
        <p>{need.why_needed || need.reason}</p>
        <div className="fact-question-context">
          {goalTitle && <span>等待目标：{goalTitle}</span>}
          <span>事实：{need.fact_key}</span>
          {need.conversation_hint && <span>上下文提示：{need.conversation_hint}</span>}
        </div>
        <div className="fact-question-answer">
          <label htmlFor={`fact-answer-${need.id}`}>你的回答</label>
          <div>
            <input
              id={`fact-answer-${need.id}`}
              value={value}
              onChange={(event) => {
                setValue(event.target.value);
                if (pending?.value !== event.target.value.trim()) setPending(null);
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  void submit();
                }
              }}
              placeholder="输入这个问题的答案"
              disabled={submitting}
            />
            <button className="button button-primary button-small" onClick={() => void submit()} disabled={submitting || !value.trim()}>
              {submitting ? <span className="button-spinner" /> : <Send size={14} />}
              {submitting ? '正在保存…' : pending ? '重试保存' : '回答并继续'}
            </button>
          </div>
        </div>
        {error && <div className="fact-question-error" role="alert"><AlertCircle size={16} /><span>{error}</span></div>}
      </div>
    </article>
  );
}

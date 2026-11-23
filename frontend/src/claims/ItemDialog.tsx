/**
 * 新建/编辑金额项：金额字符串 → 整数分实时回显；可从模拟精确值选取并带入出处。
 * 原生 dialog + 中文输入法保护；候选金额是普通按钮，不会触发表单提交。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { searchCandidates as fixtureCandidates } from './fixtures';
import { shouldBlockFormEnter } from './ime';
import { currencyMismatchMessage, formatCents, formatMoney, parseAmountToCents } from './money';
import { Modal } from './Modal';
import { SourceExcerpt } from './SourceDialog';
import { CATEGORY_LABELS, CLAIM_CATEGORIES, CURRENCY_LABELS } from './types';
import type {
  AmountCandidate, AmountCandidateSearch, ClaimCategory, ClaimCommand, ClaimCommandRunner,
  ClaimItem, ClaimSource, Currency,
} from './types';

export function ItemDialog({ item, groupId, currency, onSubmit, onClose, searchCandidates }: {
  item?: ClaimItem;
  groupId: string;
  currency: Currency;
  onSubmit: ClaimCommandRunner;
  onClose(): void;
  /** 真实案件页：GET /claims/amount-candidates；原型默认用合成精确值。 */
  searchCandidates?: AmountCandidateSearch;
}) {
  const [title, setTitle] = useState(item?.title ?? '');
  const [category, setCategory] = useState<ClaimCategory>(item?.category ?? 'principal');
  const [amountText, setAmountText] = useState(item ? formatCents(item.amount_cents) : '');
  const [calcNote, setCalcNote] = useState(item?.calc_note ?? '');
  const [source, setSource] = useState<ClaimSource | null>(item?.source ?? null);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const composing = useRef(false);
  const compositionEndedAt = useRef(0);

  const [matches, setMatches] = useState<AmountCandidate[]>(() =>
    (searchCandidates ? [] : fixtureCandidates('')));
  const [candidateError, setCandidateError] = useState('');
  // 真实候选是网络读取：停止输入 250ms 后请求，避免每敲一个字打一次接口。
  useEffect(() => {
    if (!searchCandidates) {
      setMatches(fixtureCandidates(query));
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      searchCandidates(query).then((rows) => {
        if (!cancelled) { setMatches(rows); setCandidateError(''); }
      }).catch((cause: unknown) => {
        if (!cancelled) {
          setMatches([]);
          setCandidateError(cause instanceof Error ? cause.message : '候选金额读取失败');
        }
      });
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [query, searchCandidates]);

  /** 整数分：服务端给出的分优先，否则由金额文本解析；解析失败/外币一律不入选。 */
  function candidateCents(candidate: AmountCandidate): number | null {
    if (candidate.currency !== currency) return null;
    if (candidate.amount_cents !== undefined && candidate.amount_cents !== null) {
      return candidate.amount_cents;
    }
    const parsedCandidate = parseAmountToCents(candidate.amount_text);
    return parsedCandidate.ok ? parsedCandidate.cents : null;
  }
  // 精确值只在原币种与本组一致、且能精确到分时列出；外币与高精度值不直接带入。
  const candidates = useMemo(
    () => matches.filter((candidate) => candidateCents(candidate) !== null).slice(0, 6),
    [matches, currency],
  );
  const hiddenCount = useMemo(
    () => matches.filter((candidate) => candidateCents(candidate) === null).length,
    [matches, currency],
  );
  const parsed = amountText.trim() ? parseAmountToCents(amountText) : null;
  const mismatch = parsed?.ok ? currencyMismatchMessage(parsed.currency, currency) : null;

  function choose(candidate: AmountCandidate) {
    const conflict = currencyMismatchMessage(candidate.currency, currency);
    if (conflict) { setError(conflict); return; }
    const cents = candidateCents(candidate);
    if (cents === null) {
      setError('该精确值无法换算为整数分，请手工录入金额');
      return;
    }
    // 用整数分回填（不带千分位，与手工录入的写法一致），避免字符串再解析一次造成分位误差。
    setAmountText((cents / 100).toFixed(2));
    setSource(candidate.source);
    setError('');
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!title.trim()) { setError('金额项名称不能为空'); return; }
    if (!parsed || !parsed.ok) { setError(parsed && !parsed.ok ? parsed.message : '请输入金额'); return; }
    if (mismatch) { setError(mismatch); return; }
    const command: ClaimCommand = item
      ? {
          type: 'update_item', item_id: item.id, title, category,
          amount: amountText, calc_note: calcNote, source,
        }
      : {
          type: 'add_item', group_id: groupId, title, category,
          amount: amountText, calc_note: calcNote, source,
        };
    setBusy(true);
    try {
      const result = await onSubmit(command);
      if (!result.ok) setError(result.message ?? '保存失败');
      else onClose();
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal labelledBy="claims-item-heading" className="work-dialog claims-item-dialog" onClose={onClose}>
      <form
        onSubmit={submit}
        onCompositionStart={() => { composing.current = true; }}
        onCompositionEnd={() => { composing.current = false; compositionEndedAt.current = Date.now(); }}
        onKeyDown={(event) => {
          if (shouldBlockFormEnter(event, composing.current, compositionEndedAt.current)) {
            event.preventDefault();
          }
        }}
      >
        <h2 id="claims-item-heading">{item ? '编辑金额项' : '添加金额项'}</h2>
        <p className="muted">币种随分组（{CURRENCY_LABELS[currency]}）；抵扣项在合计与瀑布图中自动按减项计算。</p>
        <label>名称
          <input autoFocus required maxLength={200} value={title}
                 onChange={(event) => setTitle(event.target.value)} />
        </label>
        <div className="claims-form-row">
          <label>类别
            <select value={category} onChange={(event) => setCategory(event.target.value as ClaimCategory)}>
              {CLAIM_CATEGORIES.map((entry) => (
                <option key={entry.value} value={entry.value}>{entry.label}</option>
              ))}
            </select>
          </label>
          <label>金额（元）
            <input inputMode="decimal" required value={amountText} placeholder="如 1,280,000.00"
                   onChange={(event) => setAmountText(event.target.value)} />
          </label>
        </div>
        {parsed ? (
          !parsed.ok
            ? <p className="field-hint claims-invalid">{parsed.message}</p>
            : mismatch
              ? <p className="field-hint claims-warning" role="note" data-testid="amount-currency-conflict">{mismatch}</p>
              : <p className="field-hint claims-cents-hint" data-testid="amount-cents">
                  = {formatMoney(parsed.cents, currency)} · {parsed.cents.toLocaleString('en-US')} 分
                </p>
        ) : <p className="field-hint">输入后显示整数分换算结果。</p>}

        <label>计算说明（可选）
          <textarea rows={3} maxLength={2000} value={calcNote}
                    onChange={(event) => setCalcNote(event.target.value)}
                    placeholder="起止日期、利率、计算基数等；首版为纯文字说明" />
        </label>

        <section className="claims-candidates" aria-label="从已抽取金额选取">
          <h3>从已抽取金额选取</h3>
          <label className="claims-candidate-search">搜索本案精确值
            <input type="search" value={query} placeholder="金额、材料名或原文"
                   onChange={(event) => setQuery(event.target.value)} />
          </label>
          {candidateError ? <p role="alert" className="field-hint claims-invalid">候选读取失败：{candidateError}</p> : null}
          {candidates.length === 0 ? (
            <p className="muted">
              {matches.length > 0 ? '没有与本组币种一致的匹配精确值。' : '没有匹配的精确值。'}
            </p>
          ) : (
            <ul className="claims-candidate-list">
              {candidates.map((candidate) => {
                const cents = candidateCents(candidate);
                return (
                  <li key={candidate.id}>
                    <button type="button" onClick={() => choose(candidate)}>
                      <span className="claims-candidate-amount">
                        {cents !== null ? formatMoney(cents, candidate.currency) : candidate.raw_text}
                      </span>
                      <span>{candidate.raw_text}</span>
                      <small>{candidate.label}</small>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {hiddenCount > 0 ? (
            <p className="muted" data-testid="candidates-hidden">
              已隐藏 {hiddenCount} 条原币种与本组（{CURRENCY_LABELS[currency]}）不一致、或无法精确到分的精确值；
              外币金额不会直接带入本组，需改用本组币种金额或调整分组币种。
            </p>
          ) : null}
        </section>

        <section className="claims-source-picker" aria-label="金额出处">
          <h3>出处</h3>
          {source ? (
            <div className="claims-chosen-source">
              <p>
                <strong>{source.material_title}</strong> · 第 {source.page} 页 · {source.locator}
              </p>
              <details>
                <summary>查看合成原文</summary>
                <SourceExcerpt source={source} />
              </details>
              <button type="button" onClick={() => setSource(null)}>
                清除出处
              </button>
            </div>
          ) : <p className="muted">未选择出处；可手工录入金额，或从上方精确值选取。</p>}
        </section>

        {error ? <p role="alert" className="claims-error">错误：{error}</p> : null}
        <div className="work-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={busy}>
            {busy ? '保存中…' : item ? '保存' : '添加'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

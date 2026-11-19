import type { Catalog, ModelEntry } from './portal-protocol';
import { pinRefusal, type ChatPin } from './portal-state';

export function providerName(row: ModelEntry): string {
  if (row.provider_label) return row.provider_label;
  const suffix = row.model ? ' / ' + row.model : '';
  return (suffix && row.label?.endsWith(suffix) ? row.label.slice(0, -suffix.length) : row.label) || row.backend || row.id;
}

/** Missing choices means an older server; an explicitly empty list stays empty. */
export function modelChoices(row: ModelEntry) {
  return (row.choices ?? (row.model ? [{ model: row.model, label: row.model, default: true }] : []))
    .filter(choice => typeof choice.model === 'string' && choice.model.trim().length > 0);
}

function defaultPick(row: ModelEntry): ChatPin | null {
  if (pinRefusal(row)) return null;
  const choice = modelChoices(row).find(choice => choice.default);
  return choice ? { backend: row.backend, model: choice.model } : null;
}

export function modelName(pin: ChatPin, catalog: Catalog | null): string {
  const rows = catalog?.models.filter(row => row.backend === pin.backend) ?? [];
  const row = rows.find(row => modelChoices(row).some(choice => choice.model === pin.model)) ?? rows[0];
  const choice = row && modelChoices(row).find(choice => choice.model === pin.model);
  return (row ? providerName(row) : pin.backend) + ' / ' + (choice?.label || pin.model);
}

export function ModelPicker({id, catalog, pin, defaultName, defaultModel, disabled, onChange, onRefresh, compact = false}: {
  id: string; catalog: Catalog | null; pin: ChatPin | null; defaultName: string | null; defaultModel?: string | null;
  disabled: boolean; onChange: (pin: ChatPin | null) => void; compact?: boolean; onRefresh?: () => void;
}) {
  const rows = catalog?.models ?? [];
  const row = pin ? rows.find(row => row.backend === pin.backend) : undefined;
  const choices = row ? modelChoices(row) : [];
  const offered = choices.some(choice => choice.model === pin?.model);
  const modelId = id + '-choice';
  return <div className={'provider-model-picker' + (compact ? ' chat-model-picker' : '')}>
    <div className="provider-model-controls">
      <label htmlFor={id}>
        <span>Provider</span>
        <select id={id} data-testid={compact ? 'model-routing-entry' : undefined} aria-label="Provider" value={pin?.backend ?? ''} disabled={disabled}
          onChange={e => {
            if (disabled) return;
            if (!e.target.value) { onChange(null); return; }
            const selected = rows.find(row => row.backend === e.target.value);
            const pick = selected?.available ? defaultPick(selected) : null;
            if (pick) onChange(pick);
          }}>
          <option value="">{!rows.length && catalog && !catalog.backends.length ? 'No model set up' : 'Default · ' + (defaultName ?? 'server/project default')}</option>
          {pin && !row && <option value={pin.backend} disabled>{pin.backend} · not offered now</option>}
          {rows.map((row, index) => {
            const reason = !row.available ? row.reason ?? 'not available right now' : pinRefusal(row) ?? (!defaultPick(row) ? 'no default model offered' : null);
            return <option key={row.backend + ':' + index} value={row.backend || 'unavailable:' + index} disabled={!!reason}>
              {providerName(row)}{reason ? ' · unavailable: ' + reason.replaceAll('_', ' ') : ''}
            </option>;
          })}
        </select>
      </label>
      <label htmlFor={modelId}>
        <span>Model for next turn</span>
        {!pin ? <output id={modelId} aria-label="Model for next turn" title={defaultModel ?? undefined}>{defaultModel ?? 'server/project default'}</output> :
          <select id={modelId} aria-label="Model for next turn" value={pin.model} disabled={disabled || !row?.available || !!pinRefusal(row) || choices.length <= 1 && offered}
            onChange={e => {
              if (disabled || !row?.available || pinRefusal(row)) return;
              const choice = choices.find(choice => choice.model === e.target.value);
              if (choice) onChange({ backend: row.backend, model: choice.model });
            }}>
            {!offered && <option value={pin.model} disabled>{pin.model} · not offered now</option>}
            {choices.map(choice => <option key={choice.model} value={choice.model}>{choice.label || choice.model}{choice.default ? ' · default' : ''}</option>)}
          </select>}
      </label>
    </div>
    {onRefresh && <button type="button" onClick={onRefresh}>Refresh models</button>}
  </div>;
}

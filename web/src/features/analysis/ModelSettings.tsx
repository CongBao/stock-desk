import {
  type Dispatch,
  type FormEvent,
  type SetStateAction,
  useEffect,
  useRef,
  useState,
} from 'react';

import type {
  AnalysisApi,
  ModelConfig,
  ModelDraft,
  ModelProvider,
} from './analysisApi';

const providerLabels: Readonly<Record<ModelProvider, string>> = {
  deepseek: 'DeepSeek',
  openai_compatible: 'OpenAI-compatible',
  ollama: 'Ollama',
};

type FormFields = {
  readonly displayName: string;
  readonly baseUrl: string;
  readonly model: string;
  readonly apiKey: string;
  readonly temperature: string;
  readonly timeout: string;
  readonly maxOutput: string;
};

const providerDefaults: Readonly<
  Record<ModelProvider, Pick<FormFields, 'baseUrl' | 'model'>>
> = {
  deepseek: {
    baseUrl: 'https://api.deepseek.com',
    model: 'deepseek-chat',
  },
  openai_compatible: { baseUrl: '', model: '' },
  ollama: {
    baseUrl: 'http://127.0.0.1:11434',
    model: 'qwen2.5:7b',
  },
};

function initialFields(provider: ModelProvider): FormFields {
  return {
    displayName: '研究模型',
    ...providerDefaults[provider],
    apiKey: '',
    temperature: '0.1',
    timeout: '90',
    maxOutput: '4096',
  };
}

export function ModelSettings({
  api,
  models,
  onModelsChange,
}: {
  readonly api: AnalysisApi;
  readonly models: readonly ModelConfig[];
  readonly onModelsChange: Dispatch<SetStateAction<readonly ModelConfig[]>>;
}) {
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState<ModelProvider>('deepseek');
  const [fields, setFields] = useState<FormFields>(() =>
    initialFields('deepseek'),
  );
  const [editing, setEditing] = useState<ModelConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [operations, setOperations] = useState<
    Readonly<Record<string, 'test' | 'disable'>>
  >({});
  const [message, setMessage] = useState('');
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const activeOperationsRef = useRef(new Set<string>());

  function close() {
    triggerRef.current?.focus();
    setOpen(false);
  }

  useEffect(() => {
    if (!open) return undefined;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== 'Tab') return;
      const dialog = dialogRef.current;
      if (dialog === null) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ),
      );
      const first = focusable[0];
      const last = focusable.at(-1);
      if (first === undefined || last === undefined) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open]);

  function updateField(name: keyof FormFields, value: string) {
    setFields((current) => ({ ...current, [name]: value }));
  }

  function selectProvider(nextProvider: ModelProvider) {
    if (nextProvider === provider) return;
    setProvider(nextProvider);
    setFields((current) => ({
      ...current,
      ...providerDefaults[nextProvider],
      apiKey: '',
    }));
  }

  function beginOperation(id: string, operation: 'test' | 'disable') {
    if (activeOperationsRef.current.has(id)) return false;
    activeOperationsRef.current.add(id);
    setOperations((current) => ({ ...current, [id]: operation }));
    return true;
  }

  function finishOperation(id: string) {
    activeOperationsRef.current.delete(id);
    setOperations((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const draft: ModelDraft = {
      displayName: fields.displayName,
      provider,
      baseUrl: fields.baseUrl || null,
      model: fields.model,
      apiKey: provider === 'ollama' ? undefined : fields.apiKey || undefined,
      temperature: Number(fields.temperature),
      timeout: Number(fields.timeout),
      maxOutput: Number(fields.maxOutput),
    };
    setSaving(true);
    setMessage('');
    try {
      const created =
        editing === null
          ? await api.createModel(draft)
          : await api.createModelSuccessor(editing.id, draft);
      onModelsChange((current) =>
        editing === null
          ? [created, ...current.filter((item) => item.id !== created.id)]
          : [created, ...current],
      );
      const wasEditing = editing !== null;
      setEditing(null);
      setProvider('deepseek');
      setFields(initialFields('deepseek'));
      setMessage(
        wasEditing
          ? '后继配置已创建，原配置保持不可变。'
          : '模型配置已安全保存，请测试连接后使用。',
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存模型配置失败');
    } finally {
      setSaving(false);
    }
  }

  async function testConnection(item: ModelConfig) {
    if (!beginOperation(item.id, 'test')) return;
    setMessage('正在测试模型连接…');
    try {
      const result = await api.testModel(item.id, item.revision);
      onModelsChange((current) =>
        current.map((model) =>
          model.id === item.id
            ? {
                ...model,
                status: result.status,
                errorCode: result.errorCode,
                revision: result.revision,
                verifiedAt: result.connected ? result.testedAt : null,
                lastTestedAt: result.lastTestedAt,
              }
            : model,
        ),
      );
      setMessage(
        result.connected
          ? '连接测试通过，模型可用于分析。'
          : '连接测试失败，请检查非敏感配置。',
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '连接测试失败');
    } finally {
      finishOperation(item.id);
    }
  }

  function edit(item: ModelConfig) {
    setEditing(item);
    setProvider(item.provider);
    setFields({
      displayName: item.displayName,
      baseUrl: item.baseUrl,
      model: item.model,
      apiKey: '',
      temperature: String(item.temperature),
      timeout: String(item.timeout),
      maxOutput: String(item.maxOutput),
    });
    setMessage('正在基于不可变配置创建后继版本。');
  }

  async function disable(item: ModelConfig) {
    if (activeOperationsRef.current.has(item.id)) return;
    if (!window.confirm(`确认禁用模型配置“${item.displayName}”？`)) return;
    if (!beginOperation(item.id, 'disable')) return;
    setMessage('正在禁用模型配置…');
    try {
      const disabled = await api.disableModel(item.id, item.revision);
      onModelsChange((current) =>
        current.map((model) => (model.id === item.id ? disabled : model)),
      );
      setMessage('模型配置已禁用，不再可用于新分析。');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '禁用模型配置失败');
    } finally {
      finishOperation(item.id);
    }
  }

  const apiKeyRequired =
    provider !== 'ollama' &&
    (editing === null ||
      editing.provider !== provider ||
      editing.baseUrl !== fields.baseUrl);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="analysis-secondary-button"
        onClick={() => setOpen(true)}
      >
        模型设置
      </button>
      {open ? (
        <div className="model-settings-backdrop" role="presentation">
          <section
            ref={dialogRef}
            className="model-settings-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="model-settings-title"
          >
            <header>
              <div>
                <span className="panel-kicker">LOW-CODE MODEL</span>
                <h3 id="model-settings-title">模型设置</h3>
              </div>
              <button
                ref={closeRef}
                type="button"
                aria-label="关闭模型设置"
                onClick={close}
              >
                ×
              </button>
            </header>
            <form onSubmit={(event) => void submit(event)}>
              <div className="model-settings-fields">
                <label>
                  提供商
                  <select
                    name="provider"
                    value={provider}
                    onChange={(event) =>
                      selectProvider(event.target.value as ModelProvider)
                    }
                  >
                    <option value="deepseek">DeepSeek</option>
                    <option value="openai_compatible">OpenAI-compatible</option>
                    <option value="ollama">Ollama</option>
                  </select>
                </label>
                <label>
                  显示名称
                  <input
                    name="displayName"
                    value={fields.displayName}
                    onChange={(event) =>
                      updateField('displayName', event.target.value)
                    }
                    required
                  />
                </label>
                <label>
                  Base URL
                  <input
                    name="baseUrl"
                    type="url"
                    value={fields.baseUrl}
                    onChange={(event) =>
                      updateField('baseUrl', event.target.value)
                    }
                    required
                  />
                </label>
                <label>
                  模型
                  <input
                    name="model"
                    value={fields.model}
                    onChange={(event) =>
                      updateField('model', event.target.value)
                    }
                    required
                  />
                </label>
                {provider === 'ollama' ? null : (
                  <label>
                    API Key
                    <input
                      name="apiKey"
                      type="password"
                      autoComplete="off"
                      value={fields.apiKey}
                      onChange={(event) =>
                        updateField('apiKey', event.target.value)
                      }
                      required={apiKeyRequired}
                    />
                  </label>
                )}
                <label>
                  Temperature
                  <input
                    name="temperature"
                    type="number"
                    min="0"
                    max="2"
                    step="0.1"
                    value={fields.temperature}
                    onChange={(event) =>
                      updateField('temperature', event.target.value)
                    }
                    required
                  />
                </label>
                <label>
                  超时（秒）
                  <input
                    name="timeout"
                    type="number"
                    min="1"
                    max="300"
                    step="1"
                    value={fields.timeout}
                    onChange={(event) =>
                      updateField('timeout', event.target.value)
                    }
                    required
                  />
                </label>
                <label>
                  最大输出 Tokens
                  <input
                    name="maxOutput"
                    type="number"
                    min="1"
                    max="65536"
                    step="1"
                    value={fields.maxOutput}
                    onChange={(event) =>
                      updateField('maxOutput', event.target.value)
                    }
                    required
                  />
                </label>
              </div>
              <button
                type="submit"
                className="analysis-primary-button"
                disabled={saving}
              >
                {saving
                  ? '正在保存…'
                  : editing === null
                    ? '保存模型配置'
                    : '创建后继配置'}
              </button>
            </form>
            <div className="saved-models" aria-label="已保存模型配置">
              <h4>已保存配置</h4>
              {models.length === 0 ? (
                <p className="analysis-empty">尚无模型配置。</p>
              ) : (
                <ul>
                  {models.map((item) => {
                    const operation = operations[item.id];
                    return (
                      <li key={item.id}>
                        <div>
                          <strong>{item.displayName}</strong>
                          <span>
                            {providerLabels[item.provider]} · {item.model}
                          </span>
                          <small>
                            <span>
                              {item.maskedApiKey ?? '本地模型 · 无密钥'}
                            </span>{' '}
                            ·{' '}
                            <span>
                              {item.status === 'verified'
                                ? '已验证'
                                : item.status === 'disabled'
                                  ? '已停用'
                                  : item.status === 'failed'
                                    ? '验证失败'
                                    : '待验证'}
                            </span>
                          </small>
                          {item.status === 'failed' &&
                          item.errorCode !== null ? (
                            <small>错误代码：{item.errorCode}</small>
                          ) : null}
                        </div>
                        <div className="saved-model-actions">
                          <button
                            type="button"
                            aria-label={`测试 ${item.displayName} 连接`}
                            disabled={
                              item.status === 'disabled' ||
                              operation !== undefined
                            }
                            onClick={() => void testConnection(item)}
                          >
                            {operation === 'test' ? '测试中…' : '测试连接'}
                          </button>
                          <button
                            type="button"
                            aria-label={`编辑 ${item.displayName}`}
                            disabled={
                              item.status === 'disabled' ||
                              operation !== undefined
                            }
                            onClick={() => edit(item)}
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            aria-label={`禁用 ${item.displayName}`}
                            disabled={
                              item.status === 'disabled' ||
                              operation !== undefined
                            }
                            onClick={() => void disable(item)}
                          >
                            {operation === 'disable' ? '禁用中…' : '禁用'}
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
            <p
              className="model-settings-message"
              role="status"
              aria-live="polite"
            >
              {message}
            </p>
          </section>
        </div>
      ) : null}
    </>
  );
}

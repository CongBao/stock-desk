import {
  type Dispatch,
  type FormEvent,
  type SetStateAction,
  useEffect,
  useRef,
  useState,
} from 'react';

import { AsyncActionButton } from '../../shared/components/AsyncActionButton';
import { ModalDialog } from '../../shared/ModalDialog';
import { safeUserMessage } from '../../shared/safeUserMessage';
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

type EditorSnapshot = {
  readonly provider: ModelProvider;
  readonly fields: FormFields;
  readonly editingId: string | null;
};

type SaveOperation = {
  readonly id: number;
  readonly session: number;
};

function editorSnapshot(
  provider: ModelProvider,
  fields: FormFields,
  editing: ModelConfig | null,
): EditorSnapshot {
  return { provider, fields, editingId: editing?.id ?? null };
}

function snapshotsMatch(left: EditorSnapshot, right: EditorSnapshot) {
  return (
    left.provider === right.provider &&
    left.editingId === right.editingId &&
    Object.keys(left.fields).every(
      (key) =>
        left.fields[key as keyof FormFields] ===
        right.fields[key as keyof FormFields],
    )
  );
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
  const [discardPending, setDiscardPending] = useState(false);
  const [disablePending, setDisablePending] = useState<ModelConfig | null>(
    null,
  );
  const [operations, setOperations] = useState<
    Readonly<Record<string, 'test' | 'disable'>>
  >({});
  const [message, setMessage] = useState('');
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const saveStatusRef = useRef<HTMLParagraphElement>(null);
  const continueEditingRef = useRef<HTMLButtonElement>(null);
  const cancelDisableRef = useRef<HTMLButtonElement>(null);
  const confirmationOriginRef = useRef<HTMLElement | null>(null);
  const baselineRef = useRef<EditorSnapshot>(
    editorSnapshot('deepseek', initialFields('deepseek'), null),
  );
  const activeOperationsRef = useRef(new Set<string>());
  const activeSaveRef = useRef<SaveOperation | null>(null);
  const editorSessionRef = useRef(0);
  const nextSaveIdRef = useRef(0);
  const mountedRef = useRef(true);

  const currentSnapshot = editorSnapshot(provider, fields, editing);
  const dirty = !snapshotsMatch(currentSnapshot, baselineRef.current);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activeSaveRef.current = null;
      editorSessionRef.current += 1;
    };
  }, []);

  useEffect(() => {
    if (saving) saveStatusRef.current?.focus();
  }, [saving]);

  function closeImmediately() {
    if (activeSaveRef.current !== null) {
      saveStatusRef.current?.focus();
      return;
    }
    editorSessionRef.current += 1;
    setDiscardPending(false);
    setDisablePending(null);
    setOpen(false);
  }

  useEffect(() => {
    if (discardPending) continueEditingRef.current?.focus();
  }, [discardPending]);

  useEffect(() => {
    if (disablePending !== null) cancelDisableRef.current?.focus();
  }, [disablePending]);

  function openSettings() {
    if (activeSaveRef.current !== null) return;
    editorSessionRef.current += 1;
    baselineRef.current = currentSnapshot;
    setDiscardPending(false);
    setDisablePending(null);
    setOpen(true);
  }

  function requestClose() {
    if (activeSaveRef.current !== null) {
      saveStatusRef.current?.focus();
      return;
    }
    if (dirty) {
      confirmationOriginRef.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      setDiscardPending(true);
      return;
    }
    closeImmediately();
  }

  function restoreEditorFocus() {
    const origin = confirmationOriginRef.current;
    confirmationOriginRef.current = null;
    window.setTimeout(() => {
      if (origin?.isConnected && !origin.matches(':disabled')) {
        origin.focus();
      }
      if (document.activeElement !== origin) closeRef.current?.focus();
    }, 0);
  }

  function returnToEditor() {
    setDiscardPending(false);
    setDisablePending(null);
    restoreEditorFocus();
  }

  function restoreBaselineAndClose() {
    const baseline = baselineRef.current;
    setProvider(baseline.provider);
    setFields(baseline.fields);
    setEditing(
      baseline.editingId === null
        ? null
        : (models.find((item) => item.id === baseline.editingId) ?? null),
    );
    closeImmediately();
  }

  function handleEscape() {
    if (activeSaveRef.current !== null) {
      saveStatusRef.current?.focus();
      return;
    }
    if (disablePending !== null) {
      returnToEditor();
      return;
    }
    if (discardPending) {
      returnToEditor();
      return;
    }
    requestClose();
  }

  function updateField(name: keyof FormFields, value: string) {
    if (activeSaveRef.current !== null) return;
    setFields((current) => ({ ...current, [name]: value }));
  }

  function selectProvider(nextProvider: ModelProvider) {
    if (activeSaveRef.current !== null) return;
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
    if (activeSaveRef.current !== null) {
      saveStatusRef.current?.focus();
      return;
    }
    const operation: SaveOperation = {
      id: nextSaveIdRef.current + 1,
      session: editorSessionRef.current,
    };
    nextSaveIdRef.current = operation.id;
    activeSaveRef.current = operation;
    const editingAtSubmission = editing;
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
    setMessage('正在保存模型配置…');
    try {
      const created =
        editingAtSubmission === null
          ? await api.createModel(draft)
          : await api.createModelSuccessor(editingAtSubmission.id, draft);
      if (
        !mountedRef.current ||
        activeSaveRef.current !== operation ||
        editorSessionRef.current !== operation.session
      )
        return;
      onModelsChange((current) =>
        editingAtSubmission === null
          ? [created, ...current.filter((item) => item.id !== created.id)]
          : [created, ...current],
      );
      const wasEditing = editingAtSubmission !== null;
      setEditing(null);
      setProvider('deepseek');
      const cleanFields = initialFields('deepseek');
      setFields(cleanFields);
      baselineRef.current = editorSnapshot('deepseek', cleanFields, null);
      setMessage(
        wasEditing
          ? '后继配置已创建，原配置保持不变。'
          : '模型配置已安全保存，请测试连接后使用。',
      );
    } catch (error) {
      if (
        !mountedRef.current ||
        activeSaveRef.current !== operation ||
        editorSessionRef.current !== operation.session
      )
        return;
      setMessage(safeUserMessage(error, '保存模型配置失败'));
    } finally {
      if (
        mountedRef.current &&
        activeSaveRef.current === operation &&
        editorSessionRef.current === operation.session
      ) {
        activeSaveRef.current = null;
        setSaving(false);
      }
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
      setMessage(safeUserMessage(error, '连接测试失败'));
    } finally {
      finishOperation(item.id);
    }
  }

  function edit(item: ModelConfig) {
    if (activeSaveRef.current !== null) return;
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
    setMessage('正在基于原配置创建后继版本。');
  }

  async function disable(item: ModelConfig) {
    if (activeOperationsRef.current.has(item.id)) return;
    if (!beginOperation(item.id, 'disable')) return;
    setDisablePending(null);
    restoreEditorFocus();
    setMessage('正在禁用模型配置…');
    try {
      const disabled = await api.disableModel(item.id, item.revision);
      onModelsChange((current) =>
        current.map((model) => (model.id === item.id ? disabled : model)),
      );
      setMessage('模型配置已禁用，不再可用于新分析。');
    } catch (error) {
      setMessage(safeUserMessage(error, '禁用模型配置失败'));
    } finally {
      finishOperation(item.id);
    }
  }

  const apiKeyRequired =
    provider !== 'ollama' &&
    (editing === null ||
      editing.provider !== provider ||
      editing.baseUrl !== fields.baseUrl);

  function requestDisable(item: ModelConfig) {
    if (activeSaveRef.current !== null) return;
    confirmationOriginRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setDisablePending(item);
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="analysis-secondary-button"
        onClick={openSettings}
      >
        模型设置
      </button>
      {open ? (
        <ModalDialog
          backdropClassName="model-settings-backdrop"
          className="model-settings-dialog"
          aria-labelledby={
            discardPending
              ? 'model-settings-discard-title'
              : disablePending !== null
                ? 'model-settings-disable-title'
                : 'model-settings-title'
          }
          initialFocusRef={closeRef}
          returnFocusRef={triggerRef}
          onEscape={handleEscape}
          aria-busy={saving}
        >
          {discardPending ? (
            <section
              role="alertdialog"
              aria-labelledby="model-settings-discard-title"
              aria-describedby="model-settings-discard-description"
            >
              <h3 id="model-settings-discard-title">放弃未保存的模型设置？</h3>
              <p id="model-settings-discard-description">
                当前字段尚未保存。继续编辑可保留这些更改。
              </p>
              <div className="saved-model-actions">
                <button
                  ref={continueEditingRef}
                  type="button"
                  onClick={returnToEditor}
                >
                  继续编辑
                </button>
                <button type="button" onClick={restoreBaselineAndClose}>
                  放弃更改
                </button>
              </div>
            </section>
          ) : null}
          {disablePending !== null ? (
            <section
              role="alertdialog"
              aria-labelledby="model-settings-disable-title"
              aria-describedby="model-settings-disable-description"
            >
              <h3 id="model-settings-disable-title">确认禁用模型配置？</h3>
              <p id="model-settings-disable-description">
                禁用“{disablePending.displayName}”后，它将不能用于新的分析。
              </p>
              <div className="saved-model-actions">
                <button
                  ref={cancelDisableRef}
                  type="button"
                  onClick={returnToEditor}
                >
                  取消禁用
                </button>
                <button
                  type="button"
                  onClick={() => void disable(disablePending)}
                >
                  确认禁用
                </button>
              </div>
            </section>
          ) : null}
          <>
            <header hidden={discardPending || disablePending !== null}>
              <div>
                <span className="panel-kicker">LOW-CODE MODEL</span>
                <h3 id="model-settings-title">模型设置</h3>
              </div>
              <button
                ref={closeRef}
                type="button"
                aria-label="关闭模型设置"
                disabled={saving}
                onClick={requestClose}
              >
                ×
              </button>
            </header>
            <form
              hidden={discardPending || disablePending !== null}
              onSubmit={(event) => void submit(event)}
            >
              <div className="model-settings-fields">
                <label>
                  提供商
                  <select
                    name="provider"
                    value={provider}
                    disabled={saving}
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
                    disabled={saving}
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
                    disabled={saving}
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
                    disabled={saving}
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
                      disabled={saving}
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
                    disabled={saving}
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
                    disabled={saving}
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
                    disabled={saving}
                    onChange={(event) =>
                      updateField('maxOutput', event.target.value)
                    }
                    required
                  />
                </label>
              </div>
              <AsyncActionButton
                type="submit"
                className="analysis-primary-button"
                pending={saving}
                disabled={saving}
              >
                {editing === null ? '保存模型配置' : '创建后继配置'}
              </AsyncActionButton>
            </form>
            <div
              className="saved-models"
              aria-label="已保存模型配置"
              hidden={discardPending || disablePending !== null}
            >
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
                          <AsyncActionButton
                            type="button"
                            aria-label={`测试 ${item.displayName} 连接`}
                            pending={operation === 'test'}
                            disabled={
                              saving ||
                              item.status === 'disabled' ||
                              operation !== undefined
                            }
                            onClick={() => void testConnection(item)}
                          >
                            测试连接
                          </AsyncActionButton>
                          <button
                            type="button"
                            aria-label={`编辑 ${item.displayName}`}
                            disabled={
                              saving ||
                              item.status === 'disabled' ||
                              operation !== undefined
                            }
                            onClick={() => edit(item)}
                          >
                            编辑
                          </button>
                          <AsyncActionButton
                            type="button"
                            aria-label={`禁用 ${item.displayName}`}
                            pending={operation === 'disable'}
                            disabled={
                              saving ||
                              item.status === 'disabled' ||
                              operation !== undefined
                            }
                            onClick={() => requestDisable(item)}
                          >
                            禁用
                          </AsyncActionButton>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
            <p
              ref={saveStatusRef}
              className="model-settings-message"
              role="status"
              aria-live="polite"
              tabIndex={-1}
              hidden={discardPending || disablePending !== null}
            >
              {message}
            </p>
          </>
        </ModalDialog>
      ) : null}
    </>
  );
}

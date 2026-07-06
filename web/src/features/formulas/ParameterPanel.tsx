import { useEffect, useId, useRef, useState } from 'react';

import type { ParameterSchema } from './formulaApi';

type ParameterInputProps = {
  readonly declaration: ParameterSchema[string];
  readonly name: string;
  readonly onValue: (value: number) => void;
};

function ParameterInput({ declaration, name, onValue }: ParameterInputProps) {
  const [text, setText] = useState(String(declaration.default));
  const [integerError, setIntegerError] = useState<string | null>(null);
  const editStartValue = useRef(declaration.default);
  const preserveInternalRollback = useRef(false);
  const errorId = useId();

  useEffect(() => {
    if (preserveInternalRollback.current) {
      preserveInternalRollback.current = false;
      return;
    }
    setText(String(declaration.default));
    setIntegerError(null);
  }, [declaration]);

  return (
    <label>
      <span>{declaration.label ?? name}</span>
      <small>
        {name} · {declaration.kind === 'integer' ? '整数' : '小数'}
      </small>
      <input
        type="number"
        aria-label={declaration.label ?? name}
        aria-describedby={integerError === null ? undefined : errorId}
        aria-invalid={integerError !== null}
        step={declaration.kind === 'integer' ? 1 : 'any'}
        value={text}
        onFocus={() => {
          editStartValue.current = declaration.default;
        }}
        onBlur={() => {
          if (text.trim().length === 0 || !Number.isFinite(Number(text))) {
            setText(String(declaration.default));
            setIntegerError(null);
          }
        }}
        onChange={(event) => {
          const next = event.currentTarget.value;
          setText(next);
          if (next.trim().length === 0) return;
          const value = Number(next);
          if (!Number.isFinite(value)) return;
          const nextIntegerError =
            declaration.kind !== 'integer'
              ? null
              : !Number.isInteger(value)
                ? '请输入整数，当前值尚未保存。'
                : !Number.isSafeInteger(value)
                  ? '请输入安全整数，当前值超出可精确保存范围。'
                  : null;
          setIntegerError(nextIntegerError);
          if (nextIntegerError !== null) {
            if (!Object.is(declaration.default, editStartValue.current)) {
              preserveInternalRollback.current = true;
              onValue(editStartValue.current);
            }
            return;
          }
          onValue(value);
        }}
      />
      {integerError === null ? null : (
        <em id={errorId} role="alert">
          {integerError}
        </em>
      )}
      {declaration.description === undefined ? null : (
        <em>{declaration.description}</em>
      )}
    </label>
  );
}

type ParameterPanelProps = {
  readonly onChange: (schema: ParameterSchema) => void;
  readonly schema: ParameterSchema;
};

export function ParameterPanel({ onChange, schema }: ParameterPanelProps) {
  const parameters = Object.entries(schema);
  const [name, setName] = useState('');
  const [kind, setKind] = useState<'integer' | 'number'>('integer');
  const [defaultText, setDefaultText] = useState('1');
  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const atLimit = parameters.length >= 64;

  const addParameter = () => {
    const normalizedName = name.trim();
    if (!/^[A-Z][A-Z0-9_]{0,63}$/u.test(normalizedName)) {
      setAddError(
        '参数名称必须以大写字母开头，只能包含大写字母、数字和下划线。',
      );
      return;
    }
    if (Object.hasOwn(schema, normalizedName)) {
      setAddError(`参数 ${normalizedName} 已存在。`);
      return;
    }
    if (atLimit) {
      setAddError('最多支持 64 个参数。');
      return;
    }
    const value = Number(defaultText);
    if (defaultText.trim().length === 0 || !Number.isFinite(value)) {
      setAddError(
        kind === 'integer' ? '默认值必须是整数。' : '默认值必须是有限数字。',
      );
      return;
    }
    if (kind === 'integer' && !Number.isInteger(value)) {
      setAddError('默认值必须是整数。');
      return;
    }
    if (kind === 'integer' && !Number.isSafeInteger(value)) {
      setAddError('默认值必须是安全整数，不能超出可精确保存范围。');
      return;
    }
    const trimmedLabel = label.trim();
    const trimmedDescription = description.trim();
    onChange({
      ...schema,
      [normalizedName]: {
        kind,
        default: value,
        ...(trimmedLabel.length > 0 ? { label: trimmedLabel } : {}),
        ...(trimmedDescription.length > 0
          ? { description: trimmedDescription }
          : {}),
      },
    });
    setName('');
    setLabel('');
    setDescription('');
    setAddError(null);
  };

  const removeParameter = (removedName: string) => {
    onChange(
      Object.fromEntries(
        Object.entries(schema).filter(
          ([candidate]) => candidate !== removedName,
        ),
      ),
    );
  };

  return (
    <section
      className="formula-parameters"
      aria-labelledby="formula-parameters-title"
    >
      <header>
        <div>
          <span className="panel-kicker">PARAMETERS</span>
          <h4 id="formula-parameters-title">参数</h4>
        </div>
        <span>{parameters.length} 项</span>
      </header>
      <div className="formula-identity-row">
        <label>
          参数名称
          <input
            aria-label="参数名称"
            maxLength={64}
            placeholder="例如 FAST"
            value={name}
            onChange={(event) => {
              setName(event.currentTarget.value);
              setAddError(null);
            }}
          />
        </label>
        <label>
          参数类型
          <select
            aria-label="参数类型"
            value={kind}
            onChange={(event) => {
              const nextKind = event.currentTarget.value as
                'integer' | 'number';
              setKind(nextKind);
              setDefaultText(nextKind === 'integer' ? '1' : '1.5');
              setAddError(null);
            }}
          >
            <option value="integer">整数</option>
            <option value="number">小数</option>
          </select>
        </label>
        <label>
          默认值
          <input
            type="number"
            aria-label="参数默认值"
            step={kind === 'integer' ? 1 : 'any'}
            value={defaultText}
            onChange={(event) => {
              setDefaultText(event.currentTarget.value);
              setAddError(null);
            }}
          />
        </label>
        <label>
          显示名称
          <input
            aria-label="显示名称"
            maxLength={64}
            value={label}
            onChange={(event) => setLabel(event.currentTarget.value)}
          />
        </label>
        <label>
          参数说明
          <input
            aria-label="参数说明"
            maxLength={256}
            value={description}
            onChange={(event) => setDescription(event.currentTarget.value)}
          />
        </label>
        <button type="button" disabled={atLimit} onClick={addParameter}>
          新增参数
        </button>
      </div>
      {atLimit ? <p>最多支持 64 个参数。</p> : null}
      {addError === null ? null : <p role="alert">{addError}</p>}
      {parameters.length === 0 ? (
        <p>此公式没有可调参数。可在上方添加，或直接校验并保存。</p>
      ) : (
        <div className="formula-parameter-grid">
          {parameters.map(([name, declaration]) => (
            <div key={name}>
              <ParameterInput
                declaration={declaration}
                name={name}
                onValue={(value) =>
                  onChange({
                    ...schema,
                    [name]: { ...declaration, default: value },
                  })
                }
              />
              <button
                type="button"
                aria-label={`删除参数 ${name}`}
                onClick={() => removeParameter(name)}
              >
                删除
              </button>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

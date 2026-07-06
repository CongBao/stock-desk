import type { FormulaSummary, FormulaVersion } from '../../formulas/formulaApi';
import { Link, useInRouterContext } from 'react-router-dom';

export type FormulaChoice = FormulaSummary & {
  readonly versions: readonly FormulaVersion[];
};

export type FormulaStepProps = {
  readonly choices: readonly FormulaChoice[];
  readonly formulaId: string;
  readonly formulaVersionId: string;
  readonly parameters: Readonly<Record<string, number>>;
  readonly onFormulaChange: (formulaId: string) => void;
  readonly onVersionChange: (versionId: string) => void;
  readonly onParameterChange: (name: string, value: number) => void;
};

export function FormulaStep({
  choices,
  formulaId,
  formulaVersionId,
  parameters,
  onFormulaChange,
  onVersionChange,
  onParameterChange,
}: FormulaStepProps) {
  const inRouter = useInRouterContext();
  const selected = choices.find((choice) => choice.id === formulaId);
  const versions = selected?.versions ?? [];
  const version = versions.find((item) => item.id === formulaVersionId);

  return (
    <section
      className="backtest-step"
      aria-labelledby="backtest-formula-heading"
    >
      <h3 id="backtest-formula-heading" tabIndex={-1}>
        1. 公式
      </h3>
      <p>选择已保存、可执行的交易公式版本，无需输入代码或版本 ID。</p>
      {choices.length === 0 ? (
        <p className="workspace-notice">
          尚无可执行交易公式。{' '}
          {inRouter ? (
            <Link to="/formulas">前往公式工作台创建并保存</Link>
          ) : (
            <a href="/formulas">前往公式工作台创建并保存</a>
          )}
        </p>
      ) : null}
      <label>
        保存的交易公式
        <select
          value={formulaId}
          onChange={(event) => onFormulaChange(event.target.value)}
        >
          <option value="">请选择公式</option>
          {choices.map((choice) => (
            <option key={choice.id} value={choice.id}>
              {choice.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        公式版本
        <select
          value={formulaVersionId}
          disabled={formulaId === ''}
          onChange={(event) => onVersionChange(event.target.value)}
        >
          <option value="">请选择版本</option>
          {versions.map((item) => (
            <option key={item.id} value={item.id}>
              v{item.version} · {item.createdAt.slice(0, 10)}
            </option>
          ))}
        </select>
      </label>
      {Object.entries(version?.parameterSchema ?? parameters).length > 0 ? (
        <fieldset className="backtest-fields">
          <legend>公式参数</legend>
          {Object.entries(version?.parameterSchema ?? {}).map(
            ([name, declaration]) => (
              <label key={name}>
                {declaration.label ?? name}
                <input
                  type="number"
                  step={declaration.kind === 'integer' ? 1 : 'any'}
                  value={parameters[name] ?? declaration.default}
                  onChange={(event) =>
                    onParameterChange(name, Number(event.target.value))
                  }
                />
              </label>
            ),
          )}
          {version === undefined
            ? Object.entries(parameters).map(([name, value]) => (
                <label key={name}>
                  {name}
                  <input
                    type="number"
                    value={value}
                    onChange={(event) =>
                      onParameterChange(name, Number(event.target.value))
                    }
                  />
                </label>
              ))
            : null}
        </fieldset>
      ) : null}
    </section>
  );
}

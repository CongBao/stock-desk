import { useMemo, useState } from 'react';

import type {
  FormulaField,
  FormulaFunction,
  FormulaTemplate,
} from './formulaApi';

const categoryLabels: Readonly<Record<FormulaFunction['category'], string>> = {
  math: '数学运算',
  logic: '逻辑判断',
  series: '序列处理',
  statistics: '统计指标',
  signal: '买卖信号',
};

type FunctionLibraryProps = {
  readonly fields: readonly FormulaField[];
  readonly functions: readonly FormulaFunction[];
  readonly templates: readonly FormulaTemplate[];
  readonly onInsert: (
    snippet: string,
    item: FormulaFunction | FormulaField,
  ) => void;
  readonly onSelectTemplate: (template: FormulaTemplate) => void;
};

export function FunctionLibrary({
  fields,
  functions,
  templates,
  onInsert,
  onSelectTemplate,
}: FunctionLibraryProps) {
  const [query, setQuery] = useState('');
  const normalizedQuery = query.trim().toUpperCase();
  const filteredTemplates = useMemo(
    () =>
      templates.filter((template) => {
        const typeLabel =
          template.formulaType === 'trading' ? '交易系统' : '技术指标';
        const placementLabel = template.placement === 'main' ? '主图' : '副图';
        return `${template.name} ${template.source} ${typeLabel} ${placementLabel}`
          .toUpperCase()
          .includes(normalizedQuery);
      }),
    [normalizedQuery, templates],
  );
  const filteredFunctions = useMemo(
    () =>
      functions.filter((item) =>
        `${item.name} ${item.signature} ${item.summaryZh} ${item.semanticsZh} ${categoryLabels[item.category]}`
          .toUpperCase()
          .includes(normalizedQuery),
      ),
    [functions, normalizedQuery],
  );
  const filteredFields = useMemo(
    () =>
      fields.filter((item) =>
        `${item.name} ${item.canonicalName} ${item.summaryZh}`
          .toUpperCase()
          .includes(normalizedQuery),
      ),
    [fields, normalizedQuery],
  );

  return (
    <aside className="formula-library" aria-label="函数与模板库">
      <header className="formula-panel-heading">
        <div>
          <span className="panel-kicker">LIBRARY / TDX-V1</span>
          <h3>函数与模板</h3>
        </div>
        <span className="formula-count">
          {functions.length + fields.length}
        </span>
      </header>
      <label className="formula-library-search">
        <span aria-hidden="true">⌕</span>
        <span className="visually-hidden">搜索函数或模板</span>
        <input
          type="search"
          aria-label="搜索函数或模板"
          placeholder="函数、字段或说明"
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
        />
      </label>

      {filteredTemplates.length > 0 ? (
        <section
          className="formula-library-section"
          aria-labelledby="template-library-title"
        >
          <h4 id="template-library-title">内置模板</h4>
          <div className="formula-template-list">
            {filteredTemplates.map((template) => (
              <button
                key={template.templateId}
                type="button"
                onClick={() => onSelectTemplate(template)}
              >
                <span className="template-glyph" aria-hidden="true">
                  ◆
                </span>
                <span>
                  <strong>{template.name}</strong>
                  <small>
                    {template.formulaType === 'trading'
                      ? '交易系统'
                      : '技术指标'}{' '}
                    ·{template.placement === 'main' ? '主图' : '副图'}
                  </small>
                </span>
              </button>
            ))}
          </div>
        </section>
      ) : null}

      {filteredFields.length > 0 ? (
        <section
          className="formula-library-section"
          aria-labelledby="field-library-title"
        >
          <h4 id="field-library-title">行情字段</h4>
          <div className="formula-entry-list">
            {filteredFields.map((field) => (
              <button
                key={field.name}
                type="button"
                aria-label={`${field.name} · ${field.summaryZh}`}
                onClick={() => onInsert(field.name, field)}
              >
                <code>{field.name}</code>
                <span>{field.summaryZh}</span>
              </button>
            ))}
          </div>
        </section>
      ) : null}

      {(Object.keys(categoryLabels) as FormulaFunction['category'][]).map(
        (category) => {
          const items = filteredFunctions.filter(
            (item) => item.category === category,
          );
          if (items.length === 0) return null;
          return (
            <section className="formula-library-section" key={category}>
              <h4>{categoryLabels[category]}</h4>
              <div className="formula-entry-list">
                {items.map((item) => (
                  <button
                    key={item.name}
                    type="button"
                    aria-label={`${item.name} · ${item.summaryZh}`}
                    title={item.semanticsZh}
                    onClick={() => onInsert(item.signature, item)}
                  >
                    <code>{item.name}</code>
                    <span>{item.summaryZh}</span>
                    <small>{item.signature}</small>
                  </button>
                ))}
              </div>
            </section>
          );
        },
      )}

      {filteredTemplates.length === 0 &&
      filteredFields.length === 0 &&
      filteredFunctions.length === 0 ? (
        <p className="formula-empty-state">没有匹配的兼容函数或字段。</p>
      ) : null}
    </aside>
  );
}

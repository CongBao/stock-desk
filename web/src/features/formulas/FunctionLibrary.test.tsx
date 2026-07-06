import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { FunctionLibrary } from './FunctionLibrary';
import type {
  FormulaField,
  FormulaFunction,
  FormulaTemplate,
} from './formulaApi';

const template: FormulaTemplate = {
  templateId: 'builtin-macd',
  name: 'MACD 金叉 / 死叉',
  formulaType: 'trading',
  placement: 'subchart',
  source: 'BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);',
  parameterSchema: {},
};

const ema: FormulaFunction = {
  category: 'statistics',
  futureBehavior: 'past_only',
  name: 'EMA',
  signature: 'EMA(系列, 周期)',
  summaryZh: '指数移动平均',
  semanticsZh: '仅使用历史数据',
  parameters: [],
};

const close: FormulaField = {
  canonicalName: 'CLOSE',
  name: 'CLOSE',
  sourceName: 'close',
  summaryZh: '收盘价序列',
  unit: 'price',
  valueType: 'number_series',
};

it.each([
  ['MACD', 'MACD 金叉 / 死叉'],
  ['交易系统', 'MACD 金叉 / 死叉'],
  ['统计指标', 'EMA'],
  ['收盘价', 'CLOSE'],
])('searches the complete library catalog for %s', async (query, expected) => {
  const user = userEvent.setup();
  render(
    <FunctionLibrary
      fields={[close]}
      functions={[ema]}
      templates={[template]}
      onInsert={vi.fn()}
      onSelectTemplate={vi.fn()}
    />,
  );

  await user.type(screen.getByRole('searchbox'), query);

  expect(screen.getByText(expected)).toBeVisible();
  expect(
    screen.queryByText('没有匹配的兼容函数或字段。'),
  ).not.toBeInTheDocument();
});

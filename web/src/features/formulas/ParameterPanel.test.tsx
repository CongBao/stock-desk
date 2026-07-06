import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';

import { ParameterPanel } from './ParameterPanel';
import type { ParameterSchema } from './formulaApi';

function ControlledPanel({ initial }: { readonly initial: ParameterSchema }) {
  const [schema, setSchema] = useState(initial);
  return (
    <>
      <output aria-label="参数 JSON">{JSON.stringify(schema)}</output>
      <ParameterPanel schema={schema} onChange={setSchema} />
    </>
  );
}

it('adds and removes a parameter through the low-code form', async () => {
  const user = userEvent.setup();
  render(<ControlledPanel initial={{}} />);

  await user.type(screen.getByRole('textbox', { name: '参数名称' }), 'FAST');
  await user.selectOptions(
    screen.getByRole('combobox', { name: '参数类型' }),
    'integer',
  );
  await user.clear(screen.getByRole('spinbutton', { name: '参数默认值' }));
  await user.type(screen.getByRole('spinbutton', { name: '参数默认值' }), '12');
  await user.type(
    screen.getByRole('textbox', { name: '显示名称' }),
    '快线周期',
  );
  await user.type(
    screen.getByRole('textbox', { name: '参数说明' }),
    '用于 EMA 快线',
  );
  await user.click(screen.getByRole('button', { name: '新增参数' }));

  expect(screen.getByLabelText('参数 JSON')).toHaveTextContent(
    '"FAST":{"kind":"integer","default":12,"label":"快线周期","description":"用于 EMA 快线"}',
  );
  expect(screen.getByRole('spinbutton', { name: '快线周期' })).toHaveValue(12);

  await user.click(screen.getByRole('button', { name: '删除参数 FAST' }));
  expect(screen.getByLabelText('参数 JSON')).toHaveTextContent('{}');
});

it('rejects invalid and duplicate names and enforces the 64 parameter limit', async () => {
  const user = userEvent.setup();
  const { rerender } = render(<ControlledPanel initial={{}} />);
  const name = screen.getByRole('textbox', { name: '参数名称' });

  await user.type(name, '1FAST');
  await user.click(screen.getByRole('button', { name: '新增参数' }));
  expect(screen.getByRole('alert')).toHaveTextContent('字母开头');

  rerender(
    <ControlledPanel
      key="duplicate"
      initial={{ FAST: { kind: 'integer', default: 12 } }}
    />,
  );
  await user.clear(screen.getByRole('textbox', { name: '参数名称' }));
  await user.type(screen.getByRole('textbox', { name: '参数名称' }), 'FAST');
  await user.click(screen.getByRole('button', { name: '新增参数' }));
  expect(screen.getByRole('alert')).toHaveTextContent('已存在');

  const maximum = Object.fromEntries(
    Array.from({ length: 64 }, (_, index) => [
      `P${String(index)}`,
      { kind: 'integer' as const, default: index },
    ]),
  );
  rerender(
    <ParameterPanel key="maximum" schema={maximum} onChange={vi.fn()} />,
  );
  expect(screen.getByRole('button', { name: '新增参数' })).toBeDisabled();
  expect(screen.getByText('最多支持 64 个参数。')).toBeVisible();
});

it('shows a fractional integer as invalid without silently saving a truncated value', async () => {
  const user = userEvent.setup();
  render(
    <ControlledPanel
      initial={{
        SHORT: { kind: 'integer', default: 12, label: '短周期' },
      }}
    />,
  );

  const input = screen.getByRole('spinbutton', { name: '短周期' });
  await user.clear(input);
  await user.type(input, '1.5');

  expect(input).toHaveValue(1.5);
  expect(input).toHaveAttribute('aria-invalid', 'true');
  expect(screen.getByText('请输入整数，当前值尚未保存。')).toBeVisible();
  expect(screen.getByLabelText('参数 JSON')).toHaveTextContent('"default":12');
});

it.each([
  [Number.MAX_SAFE_INTEGER, true],
  [Number.MIN_SAFE_INTEGER, true],
  [2 ** 53, false],
  [-(2 ** 53), false],
] as const)(
  'validates integer %s when adding a parameter (accepted=%s)',
  async (value, accepted) => {
    const user = userEvent.setup();
    render(<ControlledPanel initial={{}} />);
    fireEvent.change(screen.getByRole('textbox', { name: '参数名称' }), {
      target: { value: 'BOUNDARY' },
    });
    fireEvent.change(screen.getByRole('spinbutton', { name: '参数默认值' }), {
      target: { value: String(value) },
    });

    await user.click(screen.getByRole('button', { name: '新增参数' }));

    if (accepted) {
      expect(screen.getByLabelText('参数 JSON')).toHaveTextContent(
        `"default":${String(value)}`,
      );
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    } else {
      expect(screen.getByRole('alert')).toHaveTextContent('安全整数');
      expect(screen.getByLabelText('参数 JSON')).toHaveTextContent('{}');
    }
  },
);

it('resets an invalid local edit when a different formula supplies a new default for the same parameter', () => {
  const onChange = vi.fn();
  const { rerender } = render(
    <ParameterPanel
      schema={{
        BOUNDARY: { kind: 'integer', default: 12, label: '边界值' },
      }}
      onChange={onChange}
    />,
  );
  const input = screen.getByRole('spinbutton', { name: '边界值' });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: String(2 ** 53) } });
  expect(input).toHaveAttribute('aria-invalid', 'true');

  rerender(
    <ParameterPanel
      schema={{
        BOUNDARY: { kind: 'integer', default: 26, label: '边界值' },
      }}
      onChange={onChange}
    />,
  );

  expect(screen.getByRole('spinbutton', { name: '边界值' })).toHaveValue(26);
  expect(screen.getByRole('spinbutton', { name: '边界值' })).toHaveAttribute(
    'aria-invalid',
    'false',
  );
  expect(screen.queryByText(/安全整数/u)).not.toBeInTheDocument();
});

it('resets an invalid local edit for a new declaration with the same name and default', () => {
  const schema: ParameterSchema = {
    BOUNDARY: { kind: 'integer', default: 12, label: '边界值' },
  };
  const { rerender } = render(
    <ParameterPanel schema={schema} onChange={vi.fn()} />,
  );
  const input = screen.getByRole('spinbutton', { name: '边界值' });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: String(2 ** 53) } });
  expect(input).toHaveAttribute('aria-invalid', 'true');

  rerender(
    <ParameterPanel
      schema={{
        BOUNDARY: { kind: 'integer', default: 12, label: '边界值' },
      }}
      onChange={vi.fn()}
    />,
  );

  expect(screen.getByRole('spinbutton', { name: '边界值' })).toHaveValue(12);
  expect(screen.getByRole('spinbutton', { name: '边界值' })).toHaveAttribute(
    'aria-invalid',
    'false',
  );
});

it('resets integer validation when a new declaration changes the same parameter to number', () => {
  const { rerender } = render(
    <ParameterPanel
      schema={{
        BOUNDARY: { kind: 'integer', default: 12, label: '边界值' },
      }}
      onChange={vi.fn()}
    />,
  );
  const input = screen.getByRole('spinbutton', { name: '边界值' });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: String(2 ** 53) } });
  expect(input).toHaveAttribute('aria-invalid', 'true');

  rerender(
    <ParameterPanel
      schema={{
        BOUNDARY: { kind: 'number', default: 12, label: '边界值' },
      }}
      onChange={vi.fn()}
    />,
  );

  expect(screen.getByRole('spinbutton', { name: '边界值' })).toHaveValue(12);
  expect(screen.getByRole('spinbutton', { name: '边界值' })).toHaveAttribute(
    'aria-invalid',
    'false',
  );
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it.each([
  [Number.MAX_SAFE_INTEGER, true],
  [Number.MIN_SAFE_INTEGER, true],
  [2 ** 53, false],
  [-(2 ** 53), false],
] as const)(
  'validates integer %s when editing a parameter (accepted=%s)',
  (value, accepted) => {
    render(
      <ControlledPanel
        initial={{
          BOUNDARY: { kind: 'integer', default: 12, label: '边界值' },
        }}
      />,
    );
    const input = screen.getByRole('spinbutton', { name: '边界值' });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: String(value) } });

    if (accepted) {
      expect(screen.getByLabelText('参数 JSON')).toHaveTextContent(
        `"default":${String(value)}`,
      );
      expect(input).toHaveAttribute('aria-invalid', 'false');
    } else {
      expect(screen.getByRole('alert')).toHaveTextContent('安全整数');
      expect(screen.getByLabelText('参数 JSON')).toHaveTextContent(
        '"default":12',
      );
      expect(input).toHaveAttribute('aria-invalid', 'true');
    }
  },
);

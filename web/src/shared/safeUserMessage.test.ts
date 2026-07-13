import { safeUserMessage } from './safeUserMessage';

it('keeps a bounded user-facing explanation', () => {
  expect(
    safeUserMessage(new Error('模型配置尚未验证，请先测试连接。'), '操作失败'),
  ).toBe('模型配置尚未验证，请先测试连接。');
});

it.each([
  new Error('HTTP 503'),
  new Error('Traceback at C:\\' + 'Users\\alice\\private.txt'),
  new Error('Authorization: Bearer top-secret'),
  { message: '/home/' + 'alice/.config/stock-desk' },
  new Error('connect 127.0.0.1:49295'),
  new Error('/var/tmp/stock-desk/runtime.json'),
])('replaces unsafe technical details with stable copy', (error) => {
  expect(safeUserMessage(error, '操作暂时无法完成。')).toBe(
    '操作暂时无法完成。',
  );
});

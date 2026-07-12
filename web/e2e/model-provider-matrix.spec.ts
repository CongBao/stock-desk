import type { Route } from '@playwright/test';

import { expect, test } from './fixtures';

const now = '2026-07-08T08:00:00Z';
const digest = (character: string) => `sha256:${character.repeat(64)}`;

type ProviderDraft = {
  readonly display_name: string;
  readonly provider: 'deepseek' | 'openai_compatible' | 'ollama';
  readonly base_url: string;
  readonly model: string;
  readonly temperature: number;
  readonly timeout: number;
  readonly max_output: number;
  readonly api_key?: string;
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

test('model settings UI offers domestic providers, serializes runtime fields, and renders masked responses', async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  const saved = new Map<string, ProviderDraft>();
  const configs: Record<string, unknown>[] = [];
  const providerIds = {
    deepseek: digest('a'),
    openai_compatible: digest('b'),
    ollama: digest('c'),
  } as const;

  await page.route('**/api/settings/models**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = decodeURIComponent(url.pathname);
    const method = request.method();
    if (path.endsWith('/settings/models') && method === 'GET') {
      await json(route, { items: configs, next_cursor: null });
      return;
    }
    if (path.endsWith('/settings/models') && method === 'POST') {
      const draft = request.postDataJSON() as ProviderDraft;
      const id = providerIds[draft.provider];
      saved.set(id, structuredClone(draft));
      const apiKeyConfigured = draft.provider !== 'ollama';
      const config = {
        id,
        public_config_hash: id,
        display_name: draft.display_name,
        provider: draft.provider,
        base_url: draft.base_url,
        model: draft.model,
        temperature: draft.temperature,
        timeout: draft.timeout,
        max_output: draft.max_output,
        api_key_configured: apiKeyConfigured,
        masked_api_key: apiKeyConfigured ? 'sk-a•••••••tail' : null,
        status: 'unverified',
        revision: 0,
        verified_at: null,
        last_tested_at: null,
        error_code: null,
        supersedes_id: null,
        created_at: now,
        updated_at: now,
      };
      configs.unshift(config);
      expect(JSON.stringify(config)).not.toContain(draft.api_key ?? '__none__');
      await json(route, config, 201);
      return;
    }
    const tested = [...saved.keys()].find((id) =>
      path.endsWith(`/settings/models/${id}/test`),
    );
    if (tested !== undefined && method === 'POST') {
      const config = configs.find((item) => item['id'] === tested);
      if (config === undefined) throw new Error('missing model fixture');
      config['status'] = 'verified';
      config['revision'] = 1;
      config['verified_at'] = now;
      config['last_tested_at'] = now;
      await json(route, {
        config_id: tested,
        connected: true,
        provider: config['provider'],
        model: config['model'],
        error_code: null,
        status: 'verified',
        revision: 1,
        tested_at: now,
        last_tested_at: now,
      });
      return;
    }
    await json(route, { detail: 'not found' }, 404);
  });

  await page.goto('/analysis');
  await page.waitForTimeout(250);
  expect(pageErrors).toEqual([]);
  await page.getByRole('button', { name: '模型设置' }).click();
  const dialog = page.getByRole('dialog', { name: '模型设置' });
  await expect(dialog.getByLabel('提供商').getByRole('option')).toHaveText([
    'DeepSeek',
    'OpenAI-compatible',
    'Ollama',
  ]);
  const scenarios = [
    {
      provider: 'deepseek',
      option: 'DeepSeek',
      name: '国内 DeepSeek V4',
      baseUrl: 'https://api.deepseek.com',
      model: 'deepseek-v4',
      key: 'deepseek-matrix-secret',
      temperature: '0.2',
      timeout: '61',
      maxOutput: '5001',
    },
    {
      provider: 'openai_compatible',
      option: 'OpenAI-compatible',
      name: '国内兼容服务',
      baseUrl: 'https://llm.example.cn/v1',
      model: 'qwen-max',
      key: 'compatible-matrix-secret',
      temperature: '0.3',
      timeout: '62',
      maxOutput: '5002',
    },
    {
      provider: 'ollama',
      option: 'Ollama',
      name: '本地 Ollama',
      baseUrl: 'http://127.0.0.1:11434',
      model: 'qwen3:8b',
      temperature: '0.4',
      timeout: '63',
      maxOutput: '5003',
    },
  ] as const;

  for (const scenario of scenarios) {
    await dialog.getByLabel('提供商').selectOption(scenario.provider);
    await dialog.getByLabel('显示名称').fill(scenario.name);
    await dialog.getByLabel('Base URL').fill(scenario.baseUrl);
    await dialog.getByLabel('模型', { exact: true }).fill(scenario.model);
    if ('key' in scenario)
      await dialog.getByLabel('API Key').fill(scenario.key);
    await dialog.getByLabel('Temperature').fill(scenario.temperature);
    await dialog.getByLabel('超时（秒）').fill(scenario.timeout);
    await dialog.getByLabel('最大输出 Tokens').fill(scenario.maxOutput);
    await dialog.getByRole('button', { name: '保存模型配置' }).click();
    await expect(dialog.getByRole('status')).toContainText(
      '模型配置已安全保存',
    );
    if ('key' in scenario)
      await expect(dialog).toContainText('sk-a•••••••tail');
    await dialog
      .getByRole('button', { name: `测试 ${scenario.name} 连接` })
      .click();
    await expect(dialog.getByRole('status')).toContainText('连接测试通过');
    if ('key' in scenario)
      await expect(page.locator('body')).not.toContainText(scenario.key);
  }

  expect(saved.get(providerIds.deepseek)).toEqual({
    display_name: '国内 DeepSeek V4',
    provider: 'deepseek',
    base_url: 'https://api.deepseek.com',
    model: 'deepseek-v4',
    api_key: 'deepseek-matrix-secret',
    temperature: 0.2,
    timeout: 61,
    max_output: 5001,
  });
  expect(saved.get(providerIds.openai_compatible)).toEqual({
    display_name: '国内兼容服务',
    provider: 'openai_compatible',
    base_url: 'https://llm.example.cn/v1',
    model: 'qwen-max',
    api_key: 'compatible-matrix-secret',
    temperature: 0.3,
    timeout: 62,
    max_output: 5002,
  });
  expect(saved.get(providerIds.ollama)).toEqual({
    display_name: '本地 Ollama',
    provider: 'ollama',
    base_url: 'http://127.0.0.1:11434',
    model: 'qwen3:8b',
    temperature: 0.4,
    timeout: 63,
    max_output: 5003,
  });

  await dialog.getByRole('button', { name: '关闭模型设置' }).click();
  const selector = page.getByLabel('已验证模型');
  await expect(selector.locator('option')).toHaveCount(4);
  for (const scenario of scenarios)
    await expect(
      selector.getByRole('option', {
        name: `${scenario.name} · ${scenario.model}`,
        exact: true,
      }),
    ).toHaveCount(1);
});

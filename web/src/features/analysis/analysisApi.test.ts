import { AnalysisProtocolError, createAnalysisApi } from './analysisApi';
import type { ApiClient } from '../../shared/api/client';

const digest = `sha256:${'a'.repeat(64)}`;
const now = '2026-07-08T08:00:00Z';

function rawModel(maskedApiKey: string | null = 'sk-a•••••••tail') {
  return {
    id: digest,
    public_config_hash: digest,
    display_name: '研究模型',
    provider: 'deepseek',
    base_url: 'https://api.deepseek.com',
    model: 'deepseek-chat',
    temperature: 0.1,
    timeout: 90.0,
    max_output: 4096,
    api_key_configured: maskedApiKey !== null,
    masked_api_key: maskedApiKey,
    status: 'verified',
    revision: 1,
    verified_at: now,
    last_tested_at: now,
    error_code: null,
    supersedes_id: null,
    created_at: now,
    updated_at: now,
  };
}

it('fails closed when a model response exposes a secret field', async () => {
  const client = {
    get: vi.fn().mockResolvedValue({
      items: [{ id: 'not-a-valid-id', api_key: 'secret' }],
      next_cursor: null,
    }),
  } as unknown as ApiClient;
  const api = createAnalysisApi(client);
  await expect(api.listModels()).rejects.toBeInstanceOf(AnalysisProtocolError);
});

it.each([
  'plaintext-secret',
  'abcd••••••tail',
  'abc•••••••tail',
  'abcd•••••••tailx',
])('rejects non-protocol model mask %s', async (mask) => {
  const client = {
    get: vi.fn().mockResolvedValue({
      items: [rawModel(mask)],
      next_cursor: null,
    }),
  } as unknown as ApiClient;
  await expect(createAnalysisApi(client).listModels()).rejects.toBeInstanceOf(
    AnalysisProtocolError,
  );
});

it.each(['•••••••', '[MASKED]', 'abcd•••••••wxyz'])(
  'accepts backend protocol mask %s',
  async (mask) => {
    const client = {
      get: vi.fn().mockResolvedValue({
        items: [rawModel(mask)],
        next_cursor: null,
      }),
    } as unknown as ApiClient;
    await expect(createAnalysisApi(client).listModels()).resolves.toMatchObject(
      {
        items: [{ maskedApiKey: mask }],
      },
    );
  },
);

it('preserves strict float JSON tokens and integer max output for model writes', async () => {
  const client = {
    post: vi.fn().mockResolvedValue(rawModel()),
  } as unknown as ApiClient;
  const api = createAnalysisApi(client);

  await api.createModel({
    displayName: '研究模型',
    provider: 'deepseek',
    baseUrl: 'https://api.deepseek.com',
    model: 'deepseek-chat',
    apiKey: 'safe-test-key',
    temperature: 0,
    timeout: 90,
    maxOutput: 4096,
  });

  expect(client.post).toHaveBeenCalledWith('/settings/models', {
    serializedBody:
      '{"display_name":"研究模型","provider":"deepseek","base_url":"https://api.deepseek.com","model":"deepseek-chat","api_key":"safe-test-key","temperature":0.0,"timeout":90.0,"max_output":4096}',
    signal: undefined,
  });
});

it('parses the complete connection-test revision metadata', async () => {
  const client = {
    post: vi.fn().mockResolvedValue({
      config_id: digest,
      connected: true,
      provider: 'deepseek',
      model: 'deepseek-chat',
      error_code: null,
      status: 'verified',
      revision: 2,
      tested_at: now,
      last_tested_at: now,
    }),
  } as unknown as ApiClient;

  await expect(createAnalysisApi(client).testModel(digest, 1)).resolves.toEqual(
    {
      configId: digest,
      connected: true,
      provider: 'deepseek',
      model: 'deepseek-chat',
      errorCode: null,
      status: 'verified',
      revision: 2,
      testedAt: now,
      lastTestedAt: now,
    },
  );
});

it('rejects unsafe model error codes instead of rendering them', async () => {
  const client = {
    post: vi.fn().mockResolvedValue({
      config_id: digest,
      connected: false,
      provider: 'deepseek',
      model: 'deepseek-chat',
      error_code: '<script>secret</script>',
      status: 'failed',
      revision: 2,
      tested_at: now,
      last_tested_at: now,
    }),
  } as unknown as ApiClient;
  await expect(
    createAnalysisApi(client).testModel(digest, 1),
  ).rejects.toBeInstanceOf(AnalysisProtocolError);
});

it('maps a stable server code to a safe Chinese message without exposing raw bodies', async () => {
  const client = {
    post: vi.fn().mockRejectedValue(
      Object.assign(new Error('RAW SECRET BODY'), {
        kind: 'http',
        details: { code: 'model_not_verified', secret: 'do-not-show' },
      }),
    ),
  } as unknown as ApiClient;
  const api = createAnalysisApi(client);
  await expect(
    api.start({
      symbol: '600000.SH',
      modelConfigId: `sha256:${'a'.repeat(64)}`,
      maxRetries: 1,
    }),
  ).rejects.toThrow('所选模型尚未通过连接测试');
  await expect(
    api.start({
      symbol: '600000.SH',
      modelConfigId: `sha256:${'a'.repeat(64)}`,
      maxRetries: 1,
    }),
  ).rejects.not.toThrow(/RAW|SECRET|do-not-show/u);
});

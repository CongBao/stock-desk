import {
  ApiError,
  createApiClient,
  type ApiGetOptions,
  type ApiWriteOptions,
} from './client';

afterEach(() => {
  vi.unstubAllGlobals();
});

it('exposes GET-only request options without method or body', () => {
  expectTypeOf<ApiGetOptions>().not.toHaveProperty('method');
  expectTypeOf<ApiGetOptions>().not.toHaveProperty('body');
});

it('keeps write bodies explicit and methods fixed by the client surface', () => {
  expectTypeOf<ApiWriteOptions>().toHaveProperty('body');
  expectTypeOf<ApiWriteOptions>().not.toHaveProperty('method');
});

it.each(['put', 'post'] as const)(
  'sends bounded JSON through %s with caller headers and signal',
  async (method) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok' }), {
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    const client = createApiClient();

    await client[method]('/settings/sources', {
      body: { priorities: ['tushare'] },
      headers: { 'X-Request-Test': 'safe' },
      signal: controller.signal,
    });

    expect(fetchMock).toHaveBeenCalledWith('/api/settings/sources', {
      cache: undefined,
      credentials: undefined,
      method: method.toUpperCase(),
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-Request-Test': 'safe',
      },
      body: JSON.stringify({ priorities: ['tushare'] }),
      signal: controller.signal,
    });
  },
);

it('allows a bodyless diagnostic POST without adding a content type', async () => {
  const fetchMock = vi
    .fn<typeof fetch>()
    .mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal('fetch', fetchMock);

  await createApiClient().post('/settings/sources/baostock/test');

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/settings/sources/baostock/test',
    expect.objectContaining({
      method: 'POST',
      headers: { Accept: 'application/json' },
      body: undefined,
    }),
  );
});

it('prevents callers from overriding JSON content type with different casing', async () => {
  const fetchMock = vi
    .fn<typeof fetch>()
    .mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal('fetch', fetchMock);

  await createApiClient().put('/settings/sources', {
    body: { priorities: ['tushare'] },
    headers: { 'content-type': 'text/plain' },
  });

  const request = fetchMock.mock.calls[0]?.[1];
  expect(new Headers(request?.headers).get('content-type')).toBe(
    'application/json',
  );
});

it('returns valid JSON from the relative API base', async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
    new Response(JSON.stringify({ status: 'ok' }), {
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    }),
  );
  vi.stubGlobal('fetch', fetchMock);

  const result = await createApiClient().get('/health');

  expect(result).toEqual({ status: 'ok' });
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/health',
    expect.objectContaining({
      method: 'GET',
      headers: { Accept: 'application/json' },
    }),
  );
});

it('treats 204 as the only successful empty response', async () => {
  vi.stubGlobal(
    'fetch',
    vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(null, { status: 204 })),
  );

  await expect(createApiClient().get('/tasks')).resolves.toBeUndefined();
});

it('rejects a successful HTML response as a protocol error', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>().mockResolvedValue(
      new Response('<html>not the API</html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html' },
      }),
    ),
  );

  await expect(createApiClient().get('/health')).rejects.toMatchObject({
    name: 'ApiError',
    kind: 'protocol',
    status: 200,
    message: 'API response was not JSON',
  });
});

it('rejects malformed JSON as a protocol error', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>().mockResolvedValue(
      new Response('{not-json', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  );

  await expect(createApiClient().get('/health')).rejects.toMatchObject({
    name: 'ApiError',
    kind: 'protocol',
    status: 200,
    message: 'API response contained invalid JSON',
  });
});

it('rejects an empty non-204 JSON response as a protocol error', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>().mockResolvedValue(
      new Response('', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  );

  await expect(createApiClient().get('/health')).rejects.toMatchObject({
    name: 'ApiError',
    kind: 'protocol',
    status: 200,
    message: 'API response contained invalid JSON',
  });
});

it('preserves a problem+json HTTP error as structured details', async () => {
  const problem = {
    type: 'urn:stock-desk:unavailable',
    title: 'Data source unavailable',
  };
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(problem), {
        status: 503,
        headers: { 'Content-Type': 'application/problem+json' },
      }),
    ),
  );

  await expect(createApiClient().get('/market')).rejects.toMatchObject({
    name: 'ApiError',
    kind: 'http',
    status: 503,
    details: problem,
    message: 'API request failed with status 503',
  });
});

it('bounds text error details without exposing them in the message', async () => {
  const responseText = `upstream unavailable ${'x'.repeat(800)}`;
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>().mockResolvedValue(
      new Response(responseText, {
        status: 502,
        headers: { 'Content-Type': 'text/plain' },
      }),
    ),
  );

  const error = await createApiClient()
    .get('/market')
    .catch((reason: unknown) => reason);

  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({
    kind: 'http',
    status: 502,
    message: 'API request failed with status 502',
  });
  expect((error as ApiError).details).toHaveLength(512);
  expect((error as ApiError).details).not.toContain('x'.repeat(700));
});

it('wraps a network rejection without copying unsafe error text', async () => {
  const cause = new Error('private upstream hostname and token');
  vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockRejectedValue(cause));

  const error = await createApiClient()
    .get('/health')
    .catch((reason: unknown) => reason);

  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({
    kind: 'network',
    message: 'API request failed before receiving a response',
    cause,
  });
  expect((error as ApiError).message).not.toContain('private upstream');
});

it('distinguishes an aborted request from a network failure', async () => {
  const cause = new DOMException('request details', 'AbortError');
  vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockRejectedValue(cause));

  await expect(createApiClient().get('/tasks')).rejects.toMatchObject({
    name: 'ApiError',
    kind: 'abort',
    message: 'API request was aborted',
    cause,
  });
});

it('forwards the caller abort signal', async () => {
  const fetchMock = vi
    .fn<typeof fetch>()
    .mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal('fetch', fetchMock);
  const controller = new AbortController();

  await createApiClient().get('/tasks', { signal: controller.signal });

  expect(fetchMock).toHaveBeenCalledWith(
    '/api/tasks',
    expect.objectContaining({ signal: controller.signal }),
  );
});

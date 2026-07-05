import { ApiError, createApiClient } from './client';

afterEach(() => {
  vi.unstubAllGlobals();
});

it('requests typed JSON from the relative API base', async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
    new Response(JSON.stringify({ status: 'ok' }), {
      headers: { 'Content-Type': 'application/json' },
    }),
  );
  vi.stubGlobal('fetch', fetchMock);

  const client = createApiClient();
  const result = await client.get<{ status: string }>('/health');

  expect(result).toEqual({ status: 'ok' });
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/health',
    expect.objectContaining({ headers: { Accept: 'application/json' } }),
  );
});

it('raises a typed error for a non-JSON API failure', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>().mockResolvedValue(
      new Response('temporarily unavailable', {
        status: 503,
        headers: { 'Content-Type': 'text/plain' },
      }),
    ),
  );

  const client = createApiClient();
  const request = client.get('/health');

  await expect(request).rejects.toBeInstanceOf(ApiError);
  await expect(request).rejects.toMatchObject({
    name: 'ApiError',
    status: 503,
    message: 'API request failed with status 503',
  });
});

it('forwards an abort signal without introducing a network fallback', async () => {
  const fetchMock = vi
    .fn<typeof fetch>()
    .mockResolvedValue(new Response(null, { status: 204 }));
  vi.stubGlobal('fetch', fetchMock);
  const controller = new AbortController();

  const result = await createApiClient().get('/tasks', {
    signal: controller.signal,
  });

  expect(result).toBeUndefined();
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/tasks',
    expect.objectContaining({ signal: controller.signal }),
  );
});

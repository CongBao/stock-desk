export type ApiRequestOptions = Omit<RequestInit, 'headers' | 'method'> & {
  readonly headers?: Readonly<Record<string, string>>;
};

export class ApiError extends Error {
  readonly details: unknown;
  readonly status: number;

  constructor(status: number, details?: unknown) {
    super(`API request failed with status ${String(status)}`);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

export type ApiClient = {
  get<T>(path: string, options?: ApiRequestOptions): Promise<T | undefined>;
};

function joinApiPath(baseUrl: string, path: string): string {
  const normalizedBase = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

async function readJson(response: Response): Promise<unknown> {
  if (response.status === 204) {
    return undefined;
  }

  const contentType = response.headers.get('Content-Type') ?? '';
  if (!contentType.toLowerCase().includes('application/json')) {
    return undefined;
  }

  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

export function createApiClient(baseUrl = '/api'): ApiClient {
  return {
    async get<T>(path: string, options: ApiRequestOptions = {}) {
      const { headers, ...requestOptions } = options;
      const response = await fetch(joinApiPath(baseUrl, path), {
        ...requestOptions,
        method: 'GET',
        headers: {
          Accept: 'application/json',
          ...headers,
        },
      });
      const payload = await readJson(response);

      if (!response.ok) {
        throw new ApiError(response.status, payload);
      }

      return payload as T | undefined;
    },
  };
}

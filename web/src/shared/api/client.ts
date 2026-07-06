export type JsonValue =
  | boolean
  | null
  | number
  | string
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue };

export type ApiGetOptions = {
  readonly cache?: RequestCache;
  readonly credentials?: RequestCredentials;
  readonly headers?: Readonly<Record<string, string>>;
  readonly signal?: AbortSignal;
};

export type ApiWriteOptions = ApiGetOptions & {
  readonly body?: JsonValue;
};

export type ApiErrorKind = 'abort' | 'http' | 'network' | 'protocol';

type ApiErrorOptions = {
  readonly cause?: unknown;
  readonly details?: JsonValue | string;
  readonly kind: ApiErrorKind;
  readonly status?: number;
};

const MAX_TEXT_ERROR_DETAIL_LENGTH = 512;

export class ApiError extends Error {
  readonly details: JsonValue | string | undefined;
  readonly kind: ApiErrorKind;
  readonly status: number | undefined;

  constructor(message: string, options: ApiErrorOptions) {
    super(message, { cause: options.cause });
    this.name = 'ApiError';
    this.kind = options.kind;
    this.status = options.status;
    this.details = options.details;
  }
}

export type ApiClient = {
  readonly delete?: (
    path: string,
    options?: ApiWriteOptions,
  ) => Promise<JsonValue | undefined>;
  readonly get: (
    path: string,
    options?: ApiGetOptions,
  ) => Promise<JsonValue | undefined>;
  readonly post: (
    path: string,
    options?: ApiWriteOptions,
  ) => Promise<JsonValue | undefined>;
  readonly put: (
    path: string,
    options?: ApiWriteOptions,
  ) => Promise<JsonValue | undefined>;
};

function joinApiPath(baseUrl: string, path: string): string {
  const normalizedBase = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

function isJsonMediaType(contentType: string): boolean {
  const mediaType = contentType.split(';', 1)[0]?.trim().toLowerCase() ?? '';
  return (
    mediaType === 'application/json' ||
    (mediaType.startsWith('application/') && mediaType.endsWith('+json'))
  );
}

function isJsonValue(value: unknown): value is JsonValue {
  const pending: unknown[] = [value];
  const seen = new Set<object>();

  while (pending.length > 0) {
    const current = pending.pop();

    if (
      current === null ||
      typeof current === 'boolean' ||
      typeof current === 'string' ||
      (typeof current === 'number' && Number.isFinite(current))
    ) {
      continue;
    }

    if (typeof current !== 'object' || seen.has(current)) {
      return false;
    }

    seen.add(current);
    if (Array.isArray(current)) {
      for (const item of current as unknown[]) {
        pending.push(item);
      }
      continue;
    }

    const prototype = Object.getPrototypeOf(current) as unknown;
    if (prototype !== Object.prototype && prototype !== null) {
      return false;
    }
    const record = current as Record<string, unknown>;
    for (const item of Object.values(record)) {
      pending.push(item);
    }
  }

  return true;
}

async function parseJson(response: Response): Promise<JsonValue> {
  const value: unknown = await response.json();

  if (!isJsonValue(value)) {
    throw new TypeError('Response value is not valid JSON');
  }

  return value;
}

async function readHttpErrorDetails(
  response: Response,
): Promise<JsonValue | string | undefined> {
  if (isJsonMediaType(response.headers.get('Content-Type') ?? '')) {
    try {
      return await parseJson(response);
    } catch {
      return undefined;
    }
  }

  try {
    const text = await response.text();
    return text.length > 0
      ? text.slice(0, MAX_TEXT_ERROR_DETAIL_LENGTH)
      : undefined;
  } catch {
    return undefined;
  }
}

function isAbortFailure(error: unknown, signal?: AbortSignal): boolean {
  if (signal?.aborted) {
    return true;
  }

  return (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    error.name === 'AbortError'
  );
}

export function createApiClient(baseUrl = '/api'): ApiClient {
  async function request(
    method: 'DELETE' | 'GET' | 'POST' | 'PUT',
    path: string,
    options: ApiWriteOptions = {},
  ): Promise<JsonValue | undefined> {
    const serializedBody =
      options.body === undefined ? undefined : JSON.stringify(options.body);
    const callerHeaders = Object.fromEntries(
      Object.entries(options.headers ?? {}).filter(
        ([name]) => name.toLowerCase() !== 'content-type',
      ),
    );
    const headers: Record<string, string> = {
      Accept: 'application/json',
      ...callerHeaders,
    };
    if (serializedBody !== undefined)
      headers['Content-Type'] = 'application/json';
    let response: Response;

    try {
      response = await fetch(joinApiPath(baseUrl, path), {
        cache: options.cache,
        credentials: options.credentials,
        method,
        headers,
        body: serializedBody,
        signal: options.signal,
      });
    } catch (error) {
      if (isAbortFailure(error, options.signal)) {
        throw new ApiError('API request was aborted', {
          cause: error,
          kind: 'abort',
        });
      }

      throw new ApiError('API request failed before receiving a response', {
        cause: error,
        kind: 'network',
      });
    }

    if (!response.ok) {
      const details = await readHttpErrorDetails(response);
      throw new ApiError(
        `API request failed with status ${String(response.status)}`,
        {
          details,
          kind: 'http',
          status: response.status,
        },
      );
    }

    if (response.status === 204) return undefined;

    if (!isJsonMediaType(response.headers.get('Content-Type') ?? '')) {
      throw new ApiError('API response was not JSON', {
        kind: 'protocol',
        status: response.status,
      });
    }

    try {
      return await parseJson(response);
    } catch (error) {
      throw new ApiError('API response contained invalid JSON', {
        cause: error,
        kind: 'protocol',
        status: response.status,
      });
    }
  }

  return {
    delete(path, options = {}) {
      return request('DELETE', path, options);
    },
    get(path, options = {}) {
      return request('GET', path, options);
    },
    post(path, options = {}) {
      return request('POST', path, options);
    },
    put(path, options = {}) {
      return request('PUT', path, options);
    },
  };
}

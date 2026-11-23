/**
 * Single HTTP client for the local API.
 *
 * Rules enforced here and nowhere else:
 *  - errors are normalised to ApiError so the UI never renders a database message
 *  - requests are abortable and paginated through one helper
 */

export interface ApiErrorBody {
  code: string;
  message: string;
  details: Record<string, unknown>;
  request_id: string;
}

export class ApiError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string;
  readonly status: number;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code;
    this.details = body.details ?? {};
    this.requestId = body.request_id;
  }
}

export interface Page<T> {
  items: T[];
  offset: number;
  limit: number;
  total: number;
}

/** Messages the UI is allowed to show; everything else becomes a generic notice. */
const USER_FACING: Record<string, string> = {
  forbidden: '该操作被本地安全策略拒绝。',
  invalid_input: '输入内容不合法，请检查后重试。',
  not_found: '找不到对应记录。',
  case_mismatch: '该记录不属于当前案件。',
  conflict: '记录已被其他操作修改，请刷新后重试。',
  invalid_path: '路径不被允许。',
  unsupported_format: '该格式不支持此操作。',
  integrity_error: '文件校验失败，已阻止读取。',
  parse_failed: '解析失败，可在任务详情查看原因。',
  maintenance: '系统正在维护，请稍后重试。',
  dependency_missing: '缺少本地依赖（例如 OCR 模型），请先安装。',
  internal_error: '服务器内部错误。',
};

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return USER_FACING[error.code] ?? '请求失败。';
  }
  if (error instanceof DOMException && error.name === 'AbortError') {
    return '';
  }
  return '无法连接本地服务。';
}

async function request<T>(method: string, path: string, options: {
  body?: unknown;
  form?: FormData;
  signal?: AbortSignal;
} = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method,
    headers,
    body: options.form ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
    signal: options.signal,
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const body = (payload ?? {
      code: 'internal_error', message: '请求失败', details: {}, request_id: '',
    }) as ApiErrorBody;
    throw new ApiError(response.status, body);
  }
  return payload as T;
}

async function requestBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(path, { headers: { Accept: '*/*' }, signal });
  if (!response.ok) {
    const text = await response.text();
    let body: ApiErrorBody;
    try {
      body = JSON.parse(text) as ApiErrorBody;
    } catch {
      body = { code: 'internal_error', message: '请求失败', details: {}, request_id: '' };
    }
    throw new ApiError(response.status, body);
  }
  return response.blob();
}

async function requestText(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<string> {
  const headers: Record<string, string> = { Accept: 'text/markdown, text/csv, text/plain, application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const text = await response.text();
  if (!response.ok) {
    let parsed: ApiErrorBody;
    try {
      parsed = JSON.parse(text) as ApiErrorBody;
    } catch {
      parsed = { code: 'internal_error', message: '请求失败', details: {}, request_id: '' };
    }
    throw new ApiError(response.status, parsed);
  }
  return text;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>('GET', path, { signal }),
  post: <T>(path: string, body?: unknown, signal?: AbortSignal) =>
    request<T>('POST', path, { body, signal }),
  patch: <T>(path: string, body: unknown, signal?: AbortSignal) =>
    request<T>('PATCH', path, { body, signal }),
  upload: <T>(path: string, form: FormData, signal?: AbortSignal) =>
    request<T>('POST', path, { form, signal }),
  /** 二进制读取（原件/页预览/下载）；错误同样归一为 ApiError。 */
  blob: (path: string, signal?: AbortSignal) => requestBlob(path, signal),
  text: (method: string, path: string, body?: unknown, signal?: AbortSignal) =>
    requestText(method, path, body, signal),
};

/** Clamp pagination so the server never has to reject an obviously bad page. */
export function page(offset: number, limit: number, max = 200): { offset: number; limit: number } {
  const safeOffset = Number.isFinite(offset) ? Math.max(0, Math.trunc(offset)) : 0;
  const safeLimit = Number.isFinite(limit) ? Math.min(max, Math.max(1, Math.trunc(limit))) : 50;
  return { offset: safeOffset, limit: safeLimit };
}

export function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

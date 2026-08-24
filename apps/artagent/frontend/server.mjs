import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const rootDir = path.dirname(fileURLToPath(import.meta.url));
const distDir = path.join(rootDir, 'dist');
const port = Number(process.env.PORT || 8080);

const contentTypes = new Map([
  ['.css', 'text/css; charset=utf-8'],
  ['.gif', 'image/gif'],
  ['.html', 'text/html; charset=utf-8'],
  ['.ico', 'image/x-icon'],
  ['.jpeg', 'image/jpeg'],
  ['.jpg', 'image/jpeg'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.png', 'image/png'],
  ['.svg', 'image/svg+xml'],
  ['.webp', 'image/webp'],
  ['.woff', 'font/woff'],
  ['.woff2', 'font/woff2'],
]);

function sendJson(response, statusCode, payload, headOnly = false) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  });
  response.end(headOnly ? undefined : body);
}

function sendEasyAuthPrincipal(request, response, headOnly) {
  const encodedPrincipal = request.headers['x-ms-client-principal'];
  if (!encodedPrincipal) {
    sendJson(response, 200, [], headOnly);
    return;
  }
  try {
    const principal = JSON.parse(Buffer.from(String(encodedPrincipal), 'base64').toString('utf8'));
    sendJson(response, 200, [principal], headOnly);
  } catch {
    sendJson(response, 400, { error: 'Invalid EasyAuth principal' }, headOnly);
  }
}

async function resolveFile(pathname) {
  const requestedPath = path.normalize(path.join(distDir, pathname));
  if (!requestedPath.startsWith(distDir)) {
    return null;
  }

  try {
    const fileStat = await stat(requestedPath);
    if (fileStat.isFile()) {
      return requestedPath;
    }
    if (fileStat.isDirectory()) {
      const indexPath = path.join(requestedPath, 'index.html');
      if ((await stat(indexPath)).isFile()) {
        return indexPath;
      }
    }
  } catch {
    return path.join(distDir, 'index.html');
  }
  return path.join(distDir, 'index.html');
}

async function serveStatic(request, response, headOnly) {
  const requestUrl = new URL(request.url || '/', `http://${request.headers.host || 'localhost'}`);
  const filePath = await resolveFile(decodeURIComponent(requestUrl.pathname));
  if (!filePath) {
    response.writeHead(403).end();
    return;
  }

  const body = await readFile(filePath);
  const extension = path.extname(filePath).toLowerCase();
  const isRuntimeMutatedAsset = extension === '.js';
  response.writeHead(200, {
    'content-type': contentTypes.get(extension) || 'application/octet-stream',
    'cache-control': filePath.endsWith('index.html') || isRuntimeMutatedAsset
      ? 'no-cache'
      : 'public, max-age=31536000, immutable',
    'content-length': body.length,
  });
  response.end(headOnly ? undefined : body);
}

const server = createServer(async (request, response) => {
  try {
    const method = request.method || 'GET';
    const headOnly = method === 'HEAD';
    const requestUrl = new URL(request.url || '/', `http://${request.headers.host || 'localhost'}`);

    if (requestUrl.pathname === '/.auth/me' && (method === 'GET' || headOnly)) {
      sendEasyAuthPrincipal(request, response, headOnly);
      return;
    }

    if (method !== 'GET' && !headOnly) {
      response.writeHead(405, { allow: 'GET, HEAD' }).end();
      return;
    }

    await serveStatic(request, response, headOnly);
  } catch (error) {
    console.error('Frontend server error', error);
    response.writeHead(500).end();
  }
});

server.listen(port, '0.0.0.0', () => {
  console.log(`Frontend server listening on 0.0.0.0:${port}`);
});
import { getSandbox, Sandbox as BaseSandbox } from '@cloudflare/sandbox';

export class Sandbox extends BaseSandbox {
  enableInternet = false;
}

declare global {
  interface Env {
    EXEC_SECRET: string;
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === '/health') {
      return Response.json({ service: 'nexus-exec', healthy: true });
    }

    if (url.pathname === '/run' && request.method === 'POST') {
      if (request.headers.get('x-exec-secret') !== env.EXEC_SECRET) {
        return new Response(null, { status: 401 });
      }

      let script: string;
      let timeout_ms: number | undefined;

      try {
        const body = await request.json() as { script: string; timeout_ms?: number };
        script = body.script;
        timeout_ms = body.timeout_ms;
      } catch {
        return Response.json({ status: 'error', message: 'invalid JSON body' }, { status: 400 });
      }

      if (typeof script !== 'string' || script.length === 0 || script.length > 200000) {
        return Response.json({ status: 'error', message: 'script must be a non-empty string under 200000 chars' }, { status: 400 });
      }

      try {
        const sandbox = getSandbox(env.Sandbox, crypto.randomUUID());
        await sandbox.exec('mkdir -p /work/out');
        await sandbox.writeFile('/work/script.py', script);
        const res = await sandbox.exec('python /work/script.py', {
          timeout: Math.min(timeout_ms ?? 60000, 180000)
        });

        const ls = await sandbox.exec('ls -1 /work/out');
        const artifacts: Array<{ name: string; size_bytes: number | null; base64?: string }> = [];
        const names = ls.stdout.split('\n').filter(n => n.trim() !== '');
        for (const name of names) {
          const f = await sandbox.readFile('/work/out/' + name, { encoding: 'base64' });
          const size_bytes = f.size ?? null;
          if (size_bytes !== null && size_bytes > 25_000_000) {
            artifacts.push({ name, size_bytes });
          } else {
            artifacts.push({ name, size_bytes, base64: f.content });
          }
        }

        return Response.json({
          status: res.success ? 'ok' : 'error',
          exit_code: res.exitCode,
          stdout: res.stdout,
          stderr: res.stderr,
          artifacts,
          duration_ms: res.duration ?? null
        });
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        return Response.json({ status: 'error', message }, { status: 500 });
      }
    }

    return new Response('nexus-exec', { status: 404 });
  }
};

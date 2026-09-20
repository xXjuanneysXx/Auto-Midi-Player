/**
 * AutoPlay 曲库上传中转（Cloudflare Worker）
 * ==========================================
 *
 * 作用：你的程序往这儿 POST 一首 midi，这个 Worker 拿自己身上带的令牌替你写进
 * GitHub 仓库，顺手把 library.json 重排一遍。这样**安装包里一行令牌都没有**，
 * 但用你软件的人依旧什么都不用填、打开就能上传。
 *
 * 需要配的环境变量（Cloudflare 后台 -> 这个 Worker -> Settings -> Variables and Secrets）：
 *
 *   GITHUB_TOKEN   必填，加密变量（Secret）。细粒度 PAT，只给这一个仓库的
 *                  Contents: Read and write。
 *   OWNER          选填，默认 xXjuanneysXx
 *   REPO           选填，默认 midi-music
 *   BRANCH         选填，默认 main
 *   UPLOAD_KEY     选填，非加密也行。设了的话，请求必须带同样的口令才受理
 *                  （挡一下拿你中转当免费网盘的人）。程序那边用
 *                  `set_relay_url.py <地址> --key <口令>` 配。
 *   MAX_MB         选填，默认 2。
 *
 * 接口：
 *   GET  /           或 /health   报个状态（仓库、曲库里几首）
 *   POST /upload                 multipart 表单：file（文件）、name（文件名）、
 *                                title、artist、owner、repo、branch
 *   POST /reindex                JSON：{}，按仓库里现有的文件重排 library.json
 *
 * 返回统一是 {"ok": true/false, "told": "...", "bad": "..."}（客户端认这三个字段）。
 * 部署步骤见同目录的「部署说明.md」。
 */

const INDEX_NAME = 'library.json';
const UA = 'AutoPlay-relay/1.0';

const DEFAULTS = {
  OWNER: 'xXjuanneysXx',
  REPO: 'midi-music',
  BRANCH: 'main',
  MAX_MB: '2',
};

const MIDI_MAGIC = [0x4d, 0x54, 0x68, 0x64];   // "MThd"

function cfg(env) {
  const read = (name) => String((env && env[name]) || '').trim();
  const maxMb = Number(read('MAX_MB') || DEFAULTS.MAX_MB) || 2;
  return {
    owner: read('OWNER') || DEFAULTS.OWNER,
    repo: read('REPO') || DEFAULTS.REPO,
    branch: read('BRANCH') || DEFAULTS.BRANCH,
    token: read('GITHUB_TOKEN'),
    key: read('UPLOAD_KEY'),
    maxBytes: Math.max(1, maxMb) * 1024 * 1024,
  };
}

function reply(body, code = 200) {
  return new Response(JSON.stringify(body), {
    status: code,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}

function done(told) {
  return reply({ ok: true, told: String(told || ''), bad: '' });
}

function fail(bad, code = 200) {
  return reply({ ok: false, told: '', bad: String(bad || '失败') }, code);
}

// --------------------------------------------------------------- 小工具

function toBase64(bytes) {
  let out = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(out);
}

function fromBase64(text) {
  const bin = atob(String(text || '').replace(/\s+/g, ''));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

async function sha1hex(bytes) {
  return toHex(await crypto.subtle.digest('SHA-1', bytes));
}

function baseName(path) {
  const bits = String(path || '').replace(/\\/g, '/').split('/');
  return bits[bits.length - 1] || '';
}

function encodePath(path) {
  return String(path).split('/').map(encodeURIComponent).join('/');
}

/**
 * 把上传上来的文件名收拾成一个安全的仓库路径。
 * 只保留最后一段（不让别人往子目录里写），去掉控制字符和 Windows 的非法字符，
 * 保证 .mid / .midi 结尾，长度也截一下。
 */
function safeName(raw) {
  let name = baseName(raw).replace(/[\u0000-\u001f\u007f]/g, '');
  name = name.replace(/[<>:"|?*]/g, '_');
  name = name.replace(/^[.\s]+/, '').replace(/[.\s]+$/, '');
  name = name.replace(/\s+/g, ' ').trim();
  if (!name) return '';
  if (!/\.(mid|midi)$/i.test(name)) {
    name = name.replace(/\.[^.]*$/, '') + '.mid';
  }
  if (name.length > 100) {
    const dot = name.lastIndexOf('.');
    name = name.slice(0, 100 - (name.length - dot)) + name.slice(dot);
  }
  return name;
}

// 挡滥用：同一个人短时间传太多就先歇会儿。
// 注意这是「尽力而为」—— Cloudflare 会同时跑好几个实例，计数不共享，
// 真要严格限流得挂 KV / Durable Object。对个人曲库够了。
const HITS = new Map();

function rateLimited(ip, max = 30, windowMs = 60 * 60 * 1000) {
  const now = Date.now();
  const list = (HITS.get(ip) || []).filter((t) => now - t < windowMs);
  if (list.length >= max) {
    HITS.set(ip, list);
    return true;
  }
  list.push(now);
  HITS.set(ip, list);
  if (HITS.size > 5000) HITS.clear();          // 别把内存当无限用
  return false;
}

// --------------------------------------------------------------- GitHub

async function gh(c, path, init = {}) {
  const url = `https://api.github.com/repos/${c.owner}/${c.repo}${path}`;
  const res = await fetch(url, {
    ...init,
    headers: {
      Authorization: `Bearer ${c.token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': UA,
      ...(init.headers || {}),
    },
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (err) {
    data = null;
  }
  return { code: res.status, data, text };
}

function githubWhy(c, code, data, text) {
  const detail = (data && (data.message || data.error)) || String(text || '').slice(0, 200);
  if (code === 401 || code === 403) {
    return `GitHub 拒绝了（${code}）：令牌过期了？或者没给这个仓库的 Contents 读写权限。${detail}`;
  }
  if (code === 404) {
    return `GitHub 说找不到仓库 ${c.owner}/${c.repo}（${code}）：仓库名 / 分支名对不对？令牌有没有这个仓库？${detail}`;
  }
  return `GitHub 回了 ${code}：${detail}`;
}

async function getFile(c, path) {
  const got = await gh(c, `/contents/${encodePath(path)}?ref=${encodeURIComponent(c.branch)}`);
  if (got.code !== 200 || !got.data || !got.data.content) return null;
  return { sha: String(got.data.sha || ''), bytes: fromBase64(got.data.content) };
}

async function putFile(c, path, bytes, message, sha) {
  const payload = { message, content: toBase64(bytes), branch: c.branch };
  if (sha) payload.sha = sha;
  const got = await gh(c, `/contents/${encodePath(path)}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
  if (got.code >= 400) return githubWhy(c, got.code, got.data, got.text);
  return '';
}

async function listMidis(c) {
  const got = await gh(c, `/git/trees/${encodeURIComponent(c.branch)}?recursive=1`);
  if (got.code !== 200 || !got.data || !Array.isArray(got.data.tree)) {
    return { files: [], why: githubWhy(c, got.code, got.data, got.text) };
  }
  const files = [];
  for (const item of got.data.tree) {
    if (!item || item.type !== 'blob') continue;
    const path = String(item.path || '');
    if (!/\.(mid|midi)$/i.test(path)) continue;
    if (path.toLowerCase() === INDEX_NAME) continue;
    files.push({ path, size: Number(item.size || 0) });
  }
  files.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return { files, why: '' };
}

/**
 * 按仓库里现有的文件重排 library.json。
 * knownSha1 是刚传上去那首的 sha1；meta 是刚传上去那首的标题 / 艺术家
 * （新曲子旧索引里没有它，标题得从这儿来，不然用户填的就白填了）。
 */
async function buildIndex(c, knownSha1 = {}, meta = {}) {
  const listed = await listMidis(c);
  if (listed.why) return { told: '', bad: listed.why };
  if (!listed.files.length) return { told: '', bad: '这个仓库里一个 midi 都没有，没什么好写的' };

  const old = await getFile(c, INDEX_NAME);
  const prevByFile = {};
  if (old) {
    try {
      const parsed = JSON.parse(new TextDecoder().decode(old.bytes));
      for (const song of (parsed && parsed.songs) || []) {
        prevByFile[String(song.file || '')] = song;
      }
    } catch (err) {
      // 旧索引坏了就当没有：这次重排会把它重写成正常的
    }
  }

  const songs = [];
  for (const item of listed.files) {
    const prev = prevByFile[item.path] || {};
    const fresh = meta[item.path] || {};
    const song = {
      title: String(prev.title || fresh.title || baseName(item.path).replace(/\.[^.]+$/, '')),
      artist: String(prev.artist || fresh.artist || ''),
      file: item.path,
      size: item.size,
      sha1: '',
    };
    if (knownSha1[item.path]) song.sha1 = String(knownSha1[item.path]);
    else if (Number(prev.size || 0) === song.size) song.sha1 = String(prev.sha1 || '');
    songs.push(song);
  }

  const blob = new TextEncoder().encode(JSON.stringify({ version: 1, songs }, null, 2));
  const why = await putFile(c, INDEX_NAME, blob,
    `AutoPlay：更新曲库索引（${songs.length} 首）`, old ? old.sha : '');
  if (why) return { told: '', bad: why };
  return { told: `曲库索引已更新：共 ${songs.length} 首`, bad: '' };
}

// --------------------------------------------------------------- 接口

async function handleUpload(request, c) {
  let form = null;
  try {
    form = await request.formData();
  } catch (err) {
    return fail('请求不是 multipart 表单（是不是用浏览器直接打开的？上传要用程序里的按钮）');
  }
  const part = form.get('file');
  if (!part || typeof part === 'string' || typeof part.arrayBuffer !== 'function') {
    return fail('没收到文件');
  }
  const bytes = new Uint8Array(await part.arrayBuffer());
  if (!bytes.length) return fail('这是个空文件');
  if (bytes.length > c.maxBytes) {
    const mb = (n) => (n / 1048576).toFixed(1);
    return fail(`文件太大了（${mb(bytes.length)} MB，上限 ${mb(c.maxBytes)} MB）`);
  }
  if (!MIDI_MAGIC.every((b, i) => bytes[i] === b)) {
    return fail('这不是 midi 文件（开头不是 MThd）');
  }

  const name = safeName(form.get('name') || '');
  if (!name) return fail('文件名不合法');
  if (name.toLowerCase() === INDEX_NAME) return fail('这个名字不能用');

  const title = String(form.get('title') || '').trim().slice(0, 80);
  const artist = String(form.get('artist') || '').trim().slice(0, 80);
  const digest = await sha1hex(bytes);

  const existing = await getFile(c, name);
  if (existing && (await sha1hex(existing.bytes)) === digest) {
    return done(`${name} 已经在曲库里了（内容一模一样，没重复传）`);
  }

  const why = await putFile(c, name, bytes, `AutoPlay：上传 ${name}`,
    existing ? existing.sha : '');
  if (why) return fail(`传 ${name} 失败：${why}`);

  const known = {};
  known[name] = digest;                       // 刚传的这首，sha1 我们自己算得准
  const fresh = {};
  if (title || artist) fresh[name] = { title, artist };
  const indexed = await buildIndex(c, known, fresh);
  if (indexed.bad) {
    return done(`曲子传上去了（${name}），但索引没更新：${indexed.bad}`);
  }
  return done(`已上传 ${name}；${indexed.told}`);
}

export default {
  async fetch(request, env) {
    const c = cfg(env);
    const path = (new URL(request.url).pathname.replace(/\/+$/, '')) || '/';

    if (!c.token) {
      return fail('这个中转还没配 GITHUB_TOKEN：Cloudflare 里加一个加密变量再试', 500);
    }
    if (c.key && request.headers.get('X-AutoPlay-Key') !== c.key) {
      return fail('上传口令不对（程序里的口令和这个中转设的不一样）', 401);
    }

    if (path === '/' || path === '/health') {
      const listed = await listMidis(c);
      const where = `${c.owner}/${c.repo}@${c.branch}`;
      if (listed.why) return reply({ ok: false, told: '', bad: listed.why });
      return done(`中转活着：${where}，曲库里有 ${listed.files.length} 首`);
    }

    if (path === '/upload') {
      if (request.method !== 'POST') return fail('上传要用 POST', 405);
      const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
      if (rateLimited(ip)) return fail('传得太勤了，歇一会儿再试', 429);
      try {
        return await handleUpload(request, c);
      } catch (err) {
        return fail(`中转出错了：${err && err.message ? err.message : err}`, 500);
      }
    }

    if (path === '/reindex') {
      if (request.method !== 'POST') return fail('重排索引要用 POST', 405);
      try {
        const result = await buildIndex(c, {});
        if (result.bad) return fail(result.bad);
        return done(result.told);
      } catch (err) {
        return fail(`中转出错了：${err && err.message ? err.message : err}`, 500);
      }
    }

    return fail(`没有这个接口：${path}（能用的只有 /health、/upload、/reindex）`, 404);
  },
};
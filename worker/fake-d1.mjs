/**
 * テスト用の D1 のふり。SQLiteは使わず、必要な範囲だけを素直に実装する。
 *
 * D1 の prepare().bind().run()/first()/all() と meta.changes を再現する。
 * 本物と同じSQLを書いておくと、テストがSQLの間違いを拾えなくなるため、
 * ここでは「SQLの形」ではなく「何をしたいか」で分岐している。
 */

const clone = (row) => (row ? { ...row } : row);

export function makeD1() {
  const tables = {
    accounts: [], tokens: [], jobs: [], job_events: [],
    metrics: [], metric_attempts: [], flags: [],
  };
  let eventId = 0;

  const norm = (sql) => sql.replace(/\s+/g, " ").trim();

  function run(sql, args) {
    const text = norm(sql);
    const at = (n) => args[n - 1];

    // --- accounts ---
    if (text.startsWith("INSERT INTO accounts")) {
      const [id, platform, label, category, categoryId, externalId, settings, enabled, stamp] = args;
      const existing = tables.accounts.find((r) => r.id === id);
      const row = { id, platform, label, category, category_id: categoryId,
                    external_id: externalId, settings, enabled, created_at: stamp,
                    updated_at: stamp };
      if (existing) Object.assign(existing, row);
      else tables.accounts.push(row);
      return { meta: { changes: 1 } };
    }
    // --- tokens ---
    if (text.startsWith("INSERT INTO tokens")) {
      const [accountId, access, refresh, expires, refreshed, scope, updated] = args;
      const existing = tables.tokens.find((r) => r.account_id === accountId);
      const row = { account_id: accountId, access_token: access, refresh_token: refresh,
                    expires_at: expires, refreshed_at: refreshed, scope, updated_at: updated };
      if (existing) Object.assign(existing, row);
      else tables.tokens.push(row);
      return { meta: { changes: 1 } };
    }
    // --- jobs: 登録（同じ idempotency_key なら足さない） ---
    if (text.startsWith("INSERT INTO jobs")) {
      const [id, groupId, platform, accountId, category, subCategory, caption,
             media, mediaKind, scheduledAt, status, key, stamp] = args;
      if (tables.jobs.some((r) => r.idempotency_key === key)) {
        return { meta: { changes: 0 } };
      }
      tables.jobs.push({
        id, group_id: groupId, platform, account_id: accountId, category,
        sub_category: subCategory, caption, media, media_kind: mediaKind,
        scheduled_at: scheduledAt, status, stage: "", state: "{}", attempts: 0,
        next_attempt_at: null, lease_until: null, external_post_id: "",
        external_url: "", api_response: "", error: "", error_kind: "",
        idempotency_key: key, created_at: stamp, posted_at: null, updated_at: stamp,
      });
      return { meta: { changes: 1 } };
    }
    if (text.startsWith("INSERT INTO job_events")) {
      eventId += 1;
      tables.job_events.push({ id: eventId, job_id: args[0], at: args[1],
                               level: args[2], message: args[3] });
      return { meta: { changes: 1 } };
    }

    // --- jobs: 取り置き ---
    if (text.startsWith("UPDATE jobs SET status = ?1, lease_until = ?2")) {
      const [status, lease, stamp, id, ...allowed] = args;
      const job = tables.jobs.find((r) => r.id === id);
      if (!job) return { meta: { changes: 0 } };
      const free = !job.lease_until || job.lease_until <= stamp;
      if (!allowed.includes(job.status) || !free) return { meta: { changes: 0 } };
      Object.assign(job, { status, lease_until: lease, updated_at: stamp });
      return { meta: { changes: 1 } };
    }
    // --- jobs: 途中経過 ---
    if (text.startsWith("UPDATE jobs SET stage = ?1")) {
      const [stage, state, status, nextAttempt, stamp, id] = args;
      const job = tables.jobs.find((r) => r.id === id);
      if (!job) return { meta: { changes: 0 } };
      Object.assign(job, { stage, state, status, lease_until: null,
                           next_attempt_at: nextAttempt, updated_at: stamp });
      return { meta: { changes: 1 } };
    }
    // --- jobs: 投稿できた ---
    if (text.startsWith("UPDATE jobs SET status = ?1, external_post_id = ?2")) {
      const [status, postId, url, raw, stamp, id] = args;
      const job = tables.jobs.find((r) => r.id === id);
      if (!job) return { meta: { changes: 0 } };
      Object.assign(job, { status, external_post_id: postId, external_url: url,
                           api_response: raw, posted_at: stamp, lease_until: null,
                           stage: "", error: "", error_kind: "", updated_at: stamp });
      return { meta: { changes: 1 } };
    }
    // --- jobs: 失敗 ---
    if (text.startsWith("UPDATE jobs SET status = ?1, attempts = ?2")) {
      const [status, attempts, nextAttempt, error, kind, stamp, id] = args;
      const job = tables.jobs.find((r) => r.id === id);
      if (!job) return { meta: { changes: 0 } };
      Object.assign(job, { status, attempts, next_attempt_at: nextAttempt,
                           error, error_kind: kind, lease_until: null, updated_at: stamp });
      return { meta: { changes: 1 } };
    }
    // --- jobs: 取消 ---
    if (text.startsWith("UPDATE jobs SET status = ?1, lease_until = NULL, updated_at = ?2")) {
      const [status, stamp, id, ...allowed] = args;
      const job = tables.jobs.find((r) => r.id === id);
      if (!job || !allowed.includes(job.status)) return { meta: { changes: 0 } };
      Object.assign(job, { status, lease_until: null, updated_at: stamp });
      return { meta: { changes: 1 } };
    }
    // --- jobs: 時刻変更 ---
    if (text.startsWith("UPDATE jobs SET scheduled_at = ?1")) {
      const [when, status, stamp, id, ...allowed] = args;
      const job = tables.jobs.find((r) => r.id === id);
      if (!job) return { meta: { changes: 0 } };
      if (![status, ...allowed].includes(job.status)) return { meta: { changes: 0 } };
      Object.assign(job, { scheduled_at: when, status, next_attempt_at: null,
                           lease_until: null, updated_at: stamp });
      return { meta: { changes: 1 } };
    }
    // --- jobs: もう一度試す ---
    if (text.startsWith("UPDATE jobs SET status = ?1, attempts = 0")) {
      const [status, stamp, id, ...allowed] = args;
      const job = tables.jobs.find((r) => r.id === id);
      if (!job || !allowed.includes(job.status)) return { meta: { changes: 0 } };
      Object.assign(job, { status, attempts: 0, next_attempt_at: null, lease_until: null,
                           scheduled_at: stamp, stage: "", state: "{}", error: "",
                           error_kind: "", updated_at: stamp });
      return { meta: { changes: 1 } };
    }
    if (text.startsWith("INSERT INTO metric_attempts")) {
      tables.metric_attempts.push({ job_id: args[0], platform: args[1], snapshot: args[2],
                                    at: args[3], outcome: args[4], reason: args[5] });
      return { meta: { changes: 1 } };
    }
    if (text.startsWith("INSERT INTO metrics")) {
      const [jobId, platform, accountId, postId, category, subCategory, snapshot,
             windowHours, elapsed, late, collectedAt, publishedAt, views, reach,
             impressions, likes, comments, shares, saves, raw] = args;
      const exists = tables.metrics.some((r) => r.job_id === jobId
        && r.platform === platform && r.snapshot === snapshot);
      if (exists) return { meta: { changes: 0 } };
      tables.metrics.push({ job_id: jobId, platform, account_id: accountId,
        external_post_id: postId, category, sub_category: subCategory, snapshot,
        window_hours: windowHours, hours_since_post: elapsed, late,
        collected_at: collectedAt, published_at: publishedAt, views, reach,
        impressions, likes, comments, shares, saves, raw });
      return { meta: { changes: 1 } };
    }
    throw new Error("fake-d1: 知らないSQL: " + text.slice(0, 90));
  }

  function query(sql, args) {
    const text = norm(sql);
    if (text.startsWith("SELECT * FROM accounts WHERE id")) {
      return tables.accounts.filter((r) => r.id === args[0]);
    }
    if (text.startsWith("SELECT * FROM accounts WHERE enabled")) {
      return tables.accounts.filter((r) => r.enabled);
    }
    if (text.startsWith("SELECT * FROM tokens WHERE account_id")) {
      return tables.tokens.filter((r) => r.account_id === args[0]);
    }
    if (text.startsWith("SELECT * FROM jobs WHERE id")) {
      return tables.jobs.filter((r) => r.id === args[0]);
    }
    if (text.startsWith("SELECT * FROM jobs WHERE status IN (?1, ?2, ?3)")) {
      const [a, b, c, stamp, limit] = args;
      return tables.jobs
        .filter((r) => [a, b, c].includes(r.status)
          && r.scheduled_at <= stamp
          && (!r.next_attempt_at || r.next_attempt_at <= stamp)
          && (!r.lease_until || r.lease_until <= stamp))
        .sort((x, y) => x.scheduled_at.localeCompare(y.scheduled_at))
        .slice(0, limit);
    }
    if (text.startsWith("SELECT * FROM jobs WHERE status IN (?1, ?2) ORDER BY scheduled_at")) {
      return tables.jobs
        .filter((r) => [args[0], args[1]].includes(r.status))
        .sort((x, y) => x.scheduled_at.localeCompare(y.scheduled_at)).slice(0, 1);
    }
    if (text.startsWith("SELECT * FROM jobs WHERE substr(scheduled_at, 1, 10)")) {
      return tables.jobs.filter((r) => r.scheduled_at.slice(0, 10) === args[0]);
    }
    if (text.startsWith("SELECT * FROM jobs WHERE status = ?1 ORDER BY updated_at")) {
      return tables.jobs.filter((r) => r.status === args[0]);
    }
    if (text.startsWith("SELECT * FROM jobs WHERE status = ?1 ORDER BY posted_at")) {
      return tables.jobs.filter((r) => r.status === args[0]);
    }
    if (text.startsWith("SELECT status, COUNT(*)")) {
      const counts = {};
      for (const row of tables.jobs) counts[row.status] = (counts[row.status] || 0) + 1;
      return Object.entries(counts).map(([status, n]) => ({ status, n }));
    }
    if (text.startsWith("SELECT platform, status, COUNT(*)")) {
      const seen = {};
      for (const row of tables.jobs) {
        const key = row.platform + "|" + row.status;
        seen[key] = (seen[key] || 0) + 1;
      }
      return Object.entries(seen).map(([key, n]) => {
        const [platform, status] = key.split("|");
        return { platform, status, n };
      });
    }
    if (text.startsWith("SELECT j.account_id")) {
      const seen = {};
      for (const row of tables.jobs) {
        const key = row.account_id + "|" + row.status;
        seen[key] = (seen[key] || 0) + 1;
      }
      return Object.entries(seen).map(([key, n]) => {
        const [accountId, status] = key.split("|");
        const account = tables.accounts.find((a) => a.id === accountId);
        return { account_id: accountId, label: account?.label || "",
                 platform: account?.platform || "", status, n };
      });
    }
    if (text.startsWith("SELECT id, group_id, platform")) {
      const [a, b, since] = args;
      return tables.jobs.filter((r) => [a, b].includes(r.status)
        && (!since || r.updated_at > since));
    }
    if (text.startsWith("SELECT a.id, a.platform")) {
      return tables.accounts.map((a) => ({
        ...a,
        expires_at: tables.tokens.find((t) => t.account_id === a.id)?.expires_at || "",
        has_token: tables.tokens.some((t) => t.account_id === a.id) ? 1 : 0,
      }));
    }
    if (text.startsWith("SELECT at, level, message FROM job_events")) {
      return tables.job_events.filter((r) => r.job_id === args[0]).reverse();
    }
    if (text.startsWith("SELECT j.*, a.external_id")) {
      return tables.jobs
        .filter((r) => r.status === "posted" && r.posted_at && r.external_post_id)
        .map((r) => {
          const account = tables.accounts.find((a) => a.id === r.account_id);
          return { ...r, external_id: account?.external_id || "",
                   account_platform: account?.platform || "" };
        });
    }
    if (text.startsWith("SELECT snapshot FROM metrics")) {
      return tables.metrics
        .filter((r) => r.job_id === args[0] && r.platform === args[1])
        .map((r) => ({ snapshot: r.snapshot }));
    }
    if (text.startsWith("SELECT * FROM metrics")) {
      return tables.metrics.filter((r) => !args[0] || r.collected_at > args[0]);
    }
    throw new Error("fake-d1: 知らないSELECT: " + text.slice(0, 90));
  }

  const prepare = (sql) => {
    let bound = [];
    const api = {
      bind(...args) { bound = args; return api; },
      async run() { return run(sql, bound); },
      async first() { return clone(query(sql, bound)[0] || null); },
      async all() { return { results: query(sql, bound).map(clone) }; },
    };
    return api;
  };

  return { prepare, _tables: tables };
}

/* 演示模式：不连后端，全部数据在浏览器里编造并存在 localStorage。
 *
 * 用法：
 *   DemoMode.enter()     进入演示（灌入样例数据，返回演示账号信息）
 *   DemoMode.leave()     退出演示，清掉演示数据
 *   DemoMode.enabled     当前是否在演示
 *   DemoMode.request(path, options)  apiRequest 的替身
 *
 * 字段按 server.py 的真实返回结构造，前端不用为演示模式改渲染逻辑。
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'collabnote-demo-db';
  const ME = 'demo';
  const LATENCY = 90;

  let on = false;
  let db = null;

  // ────────── 样例数据 ──────────

  function daysAgo(n, hour = 12) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    d.setHours(hour, 0, 0, 0);
    return d.toISOString();
  }

  function seed() {
    const notes = [
      {
        id: 1,
        title: '产品路线图 · 2026 下半年',
        tag: '规划',
        tagStyle: 'background:#eef4ff;color:#2563eb;',
        content:
          '<p><b>目标</b>：把「笔记 + 账本」打磨成一套能直接交付的协作工具。</p>' +
          '<p>本季度分三步走：</p>' +
          '<ol><li>补齐账本权限与历史还原</li><li>把同步冲突的处理做成提示而不是覆盖</li><li>打包成免安装版本，交给非技术同学试用</li></ol>' +
          '<blockquote>先能用，再好看。</blockquote>',
        time: '刚刚',
        owner: ME,
        ownerName: '演示用户',
        members: ['lin', 'zhou'],
        viewers: ['chen'],
        access: 'owner',
        canEdit: true,
        canManage: true
      },
      {
        id: 2,
        title: '周会纪要 · 11 月第 1 周',
        tag: '会议',
        tagStyle: 'background:#fdf2e9;color:#c2410c;',
        content:
          '<p><b>时间</b>：周一 10:00　<b>参与</b>：全员</p>' +
          '<ul><li>账本模块上线后，反馈集中在「预算进度跟着周期跳」</li><li>决定把预算文案改成按当前周期计算</li><li>下周补一版演示数据，方便对外展示</li></ul>' +
          '<p>待办：<u>演示模式</u>由我推进。</p>',
        time: '2 小时前',
        owner: 'lin',
        ownerName: '林一',
        members: [ME],
        viewers: [],
        access: 'edit',
        canEdit: true,
        canManage: false
      },
      {
        id: 3,
        title: '设计稿评审记录',
        tag: '设计',
        tagStyle: 'background:#f3e8ff;color:#7e22ce;',
        content:
          '<p>首页的三栏比例定在 <b>280 / 自适应 / 320</b>，窄屏时右栏收起。</p>' +
          '<p>色板上限五个，浅色模式可选底色，深色模式不给选。</p>' +
          '<blockquote>留白不是空的，是让人喘口气的地方。</blockquote>',
        time: '昨天',
        owner: 'zhou',
        ownerName: '周聿',
        members: [ME, 'chen'],
        viewers: [],
        access: 'edit',
        canEdit: true,
        canManage: false
      },
      {
        id: 4,
        title: '读《设计中的设计》',
        tag: '随笔',
        tagStyle: 'background:#ecfdf5;color:#047857;',
        content:
          '<p>原研哉说，设计不是造出漂亮的东西，是把已知的事物重新看见。</p>' +
          '<p>放到软件里也一样：一个按钮放哪儿，比它什么颜色重要得多。</p>',
        time: '3 天前',
        owner: ME,
        ownerName: '演示用户',
        members: [],
        viewers: [],
        access: 'owner',
        canEdit: true,
        canManage: true
      },
      {
        id: 5,
        title: '报销清单（11 月）',
        tag: '财务',
        tagStyle: 'background:#fef9c3;color:#a16207;',
        content:
          '<table><tbody>' +
          '<tr><th>项目</th><th>金额</th></tr>' +
          '<tr><td>差旅 · 高铁</td><td>¥ 386</td></tr>' +
          '<tr><td>住宿 · 两晚</td><td>¥ 520</td></tr>' +
          '<tr><td>餐饮</td><td>¥ 168</td></tr>' +
          '</tbody></table>' +
          '<p>合计 <b>¥ 1074</b>，已交财务。</p>',
        time: '上周',
        owner: 'chen',
        ownerName: '陈迟',
        members: [ME],
        viewers: [],
        access: 'view',
        canEdit: false,
        canManage: false
      }
    ];

    const friends = [
      { username: 'lin', displayName: '林一', title: '产品', avatarUrl: null, online: true },
      { username: 'zhou', displayName: '周聿', title: '设计', avatarUrl: null, online: true },
      { username: 'chen', displayName: '陈迟', title: '前端', avatarUrl: null, online: false }
    ];

    const ledgers = [
      {
        id: 1,
        name: '家庭账本',
        icon: 'fa-house',
        budget: 6000,
        owner: ME,
        role: 'owner',
        members: [
          { username: ME, displayName: '演示用户', role: 'owner' },
          { username: 'lin', displayName: '林一', role: 'editor' },
          { username: 'zhou', displayName: '周聿', role: 'viewer' }
        ],
        transactions: [
          { id: 11, type: 'expense', amount: 35, category: '餐饮', note: '午餐 · 猪脚饭', occurredAt: daysAgo(0, 12), createdBy: ME, paidBy: ME, splitMembers: [], deleted: false },
          { id: 10, type: 'expense', amount: 268, category: '日用', note: '超市采购', occurredAt: daysAgo(1, 19), createdBy: ME, paidBy: ME, splitMembers: [], deleted: false },
          { id: 9, type: 'income', amount: 9800, category: '工资', note: '十月工资', occurredAt: daysAgo(3, 10), createdBy: ME, paidBy: ME, splitMembers: [], deleted: false },
          { id: 8, type: 'expense', amount: 120, category: '交通', note: '打车', occurredAt: daysAgo(4, 21), createdBy: 'lin', paidBy: 'lin', splitMembers: [ME, 'lin'], deleted: false },
          { id: 7, type: 'expense', amount: 46, category: '餐饮', note: '早餐 · 肠粉', occurredAt: daysAgo(5, 8), createdBy: ME, paidBy: ME, splitMembers: [], deleted: false },
          { id: 6, type: 'expense', amount: 89, category: '娱乐', note: '电影票', occurredAt: daysAgo(6, 20), createdBy: 'zhou', paidBy: 'zhou', splitMembers: [ME, 'zhou'], deleted: false },
          { id: 5, type: 'expense', amount: 320, category: '日用', note: '换灯泡 + 收纳箱', occurredAt: daysAgo(9, 15), createdBy: ME, paidBy: ME, splitMembers: [], deleted: false },
          { id: 4, type: 'expense', amount: 58, category: '餐饮', note: '外卖', occurredAt: daysAgo(12, 19), createdBy: ME, paidBy: ME, splitMembers: [], deleted: false }
        ],
        history: []
      },
      {
        id: 2,
        name: '旅行基金',
        icon: 'fa-plane',
        budget: 3000,
        owner: 'lin',
        role: 'editor',
        members: [
          { username: 'lin', displayName: '林一', role: 'owner' },
          { username: ME, displayName: '演示用户', role: 'editor' }
        ],
        transactions: [
          { id: 21, type: 'expense', amount: 780, category: '交通', note: '往返高铁', occurredAt: daysAgo(2, 9), createdBy: 'lin', paidBy: 'lin', splitMembers: [ME, 'lin'], deleted: false },
          { id: 22, type: 'expense', amount: 420, category: '住宿', note: '民宿一晚', occurredAt: daysAgo(2, 20), createdBy: ME, paidBy: ME, splitMembers: [ME, 'lin'], deleted: false },
          { id: 23, type: 'expense', amount: 96, category: '餐饮', note: '海边小店', occurredAt: daysAgo(1, 13), createdBy: ME, paidBy: ME, splitMembers: [ME, 'lin'], deleted: false },
          { id: 24, type: 'income', amount: 1000, category: '转入', note: '各存五百', occurredAt: daysAgo(7, 10), createdBy: 'lin', paidBy: 'lin', splitMembers: [], deleted: false }
        ],
        history: []
      }
    ];

    return {
      version: 1,
      user: { username: ME, displayName: '演示用户', title: '产品演示' },
      notes,
      friends,
      incoming: [
        { username: 'wu', displayName: '吴桐', title: '运营', avatarUrl: null, online: false }
      ],
      outgoing: [],
      ledgers,
      seq: { note: 100, ledger: 10, txn: 100, history: 1 }
    };
  }

  // ────────── 存取 ──────────

  function load() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return parsed && parsed.version === 1 ? parsed : null;
    } catch (error) {
      return null;
    }
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(db));
    } catch (error) {
      /* 存不下就算了，内存里还能用 */
    }
  }

  function delay(value) {
    return new Promise(resolve => setTimeout(() => resolve(value), LATENCY));
  }

  function fail(status, message) {
    const error = new Error(message || `演示模式 ${status}`);
    error.status = status;
    return Promise.reject(error);
  }

  // ────────── 工具 ──────────

  function findLedger(id) {
    return db.ledgers.find(l => String(l.id) === String(id) && !l.deleted) || null;
  }

  function visibleNotes() {
    return db.notes.filter(n => n.access !== 'none');
  }

  function roleRank(role) {
    return { owner: 4, admin: 3, editor: 2, viewer: 1 }[role] || 0;
  }

  function settlement(ledger) {
    const net = new Map();
    const bump = (name, delta) => net.set(name, Math.round(((net.get(name) || 0) + delta) * 100) / 100);
    ledger.transactions.filter(t => !t.deleted && t.type === 'expense' && t.splitMembers && t.splitMembers.length).forEach(t => {
      const people = t.splitMembers;
      const share = Math.round((t.amount / people.length) * 100) / 100;
      bump(t.paidBy, t.amount);
      people.forEach(p => bump(p, -share));
    });
    const debtors = [...net.entries()].filter(([, v]) => v < -0.01).map(([k, v]) => [k, -v]).sort((a, b) => b[1] - a[1]);
    const creditors = [...net.entries()].filter(([, v]) => v > 0.01).map(([k, v]) => [k, v]).sort((a, b) => b[1] - a[1]);
    const transfers = [];
    let i = 0;
    let j = 0;
    while (i < debtors.length && j < creditors.length) {
      const amount = Math.min(debtors[i][1], creditors[j][1]);
      if (amount > 0.01) {
        transfers.push({ from: debtors[i][0], to: creditors[j][0], amount: Math.round(amount * 100) / 100 });
      }
      debtors[i][1] -= amount;
      creditors[j][1] -= amount;
      if (debtors[i][1] < 0.01) i += 1;
      if (creditors[j][1] < 0.01) j += 1;
    }
    return transfers;
  }

  // ────────── 路由 ──────────

  function handle(path, options) {
    const method = (options && options.method) || 'GET';
    const [rawPath, rawQuery] = String(path).split('?');
    const query = new URLSearchParams(rawQuery || '');
    const body = options && options.body ? safeParse(options.body) : {};
    const parts = rawPath.split('/').filter(Boolean); // ['api', ...]

    if (rawPath === '/api/health') return delay({ status: 'ok', demo: true });

    if (rawPath === '/api/auth/login' || rawPath === '/api/auth/register') {
      return delay({ token: 'demo-token', sid: 'demo-sid', ...publicMe() });
    }
    if (rawPath === '/api/auth/me') return delay(publicMe());
    if (rawPath === '/api/auth/logout') return delay({ ok: true });
    if (rawPath === '/api/auth/profile') {
      if (body.displayName) db.user.displayName = String(body.displayName).slice(0, 32);
      if (body.title) db.user.title = String(body.title).slice(0, 40);
      persist();
      return delay(publicMe());
    }

    if (parts[1] === 'notes') return handleNotes(parts, method, body, query);
    if (parts[1] === 'friends') return handleFriends(parts, method, body);
    if (parts[1] === 'ledgers') return handleLedgers(parts, method, body, query);
    if (rawPath === '/api/presence') {
      return delay({ people: [{ username: ME, displayName: db.user.displayName, avatarUrl: null, mode: body.mode || 'viewing' }] });
    }
    return fail(404, '演示模式没有这个接口：' + rawPath);
  }

  function safeParse(text) {
    try {
      return typeof text === 'string' ? JSON.parse(text) : (text || {});
    } catch (error) {
      return {};
    }
  }

  function publicMe() {
    return {
      username: db.user.username,
      displayName: db.user.displayName,
      title: db.user.title,
      avatarUrl: null
    };
  }

  // ────────── 笔记 ──────────

  function handleNotes(parts, method, body, query) {
    // /api/notes
    if (parts.length === 2) {
      if (method === 'GET') return delay(visibleNotes());
      if (method === 'POST') {
        const note = {
          id: ++db.seq.note,
          title: String(body.title || '未命名笔记').slice(0, 255),
          tag: '草稿',
          tagStyle: 'background:#f1f5f9;color:#475569;',
          content: String(body.content || ''),
          time: '刚刚',
          owner: ME,
          ownerName: db.user.displayName,
          members: [],
          viewers: [],
          access: 'owner',
          canEdit: true,
          canManage: true
        };
        db.notes.unshift(note);
        persist();
        return delay(note);
      }
    }

    const note = db.notes.find(n => String(n.id) === String(parts[2]));
    if (!note) return fail(404, '演示模式里没有这篇笔记');

    // /api/notes/:id
    if (parts.length === 3) {
      if (method === 'PUT') {
        if (body.title !== undefined) note.title = String(body.title).slice(0, 255);
        if (body.content !== undefined) note.content = String(body.content);
        note.time = '刚刚';
        persist();
        return delay(note);
      }
      if (method === 'DELETE') {
        db.notes = db.notes.filter(n => n !== note);
        persist();
        return delay({ ok: true });
      }
    }

    // /api/notes/:id/members
    if (parts[3] === 'members') {
      if (method === 'GET') {
        return delay({
          owner: note.owner,
          ownerName: note.ownerName,
          members: note.members.slice(),
          viewers: note.viewers.slice(),
          canManage: note.canManage
        });
      }
      if (method === 'POST') {
        const username = String(body.username || '').trim();
        const role = body.access === 'view' ? 'viewers' : 'members';
        const other = role === 'viewers' ? 'members' : 'viewers';
        if (username && username !== note.owner) {
          note[other] = note[other].filter(u => u !== username);
          if (!note[role].includes(username)) note[role].push(username);
          persist();
        }
        return delay({ owner: note.owner, members: note.members.slice(), viewers: note.viewers.slice() });
      }
      if (method === 'DELETE') {
        const username = String(parts[4] || '');
        note.members = note.members.filter(u => u !== username);
        note.viewers = note.viewers.filter(u => u !== username);
        persist();
        return delay({ owner: note.owner, members: note.members.slice(), viewers: note.viewers.slice() });
      }
    }

    return fail(404, '演示模式不支持的笔记操作');
  }

  // ────────── 好友 ──────────

  function handleFriends(parts, method, body) {
    if (parts.length === 2) {
      if (method === 'GET') {
        return delay({
          friends: db.friends.slice(),
          incomingRequests: db.incoming.slice(),
          outgoingRequests: db.outgoing.slice()
        });
      }
      if (method === 'POST') {
        const username = String(body.username || '').trim();
        if (username && !db.outgoing.some(u => u.username === username)) {
          db.outgoing.push({ username, displayName: username, title: '待确认', avatarUrl: null, online: false });
          persist();
        }
        return delay({ ok: true });
      }
    }

    if (parts[2] === 'requests' && parts[4]) {
      const username = String(parts[3]);
      db.incoming = db.incoming.filter(u => u.username !== username);
      if (parts[4] === 'accept') {
        db.friends.push({ username, displayName: username, title: '新朋友', avatarUrl: null, online: true });
      }
      persist();
      return delay({ ok: true });
    }
    return fail(404, '演示模式不支持的好友操作');
  }

  // ────────── 账本 ──────────

  function handleLedgers(parts, method, body, query) {
    if (parts.length === 2) {
      if (method === 'GET') {
        return delay(db.ledgers.filter(l => !l.deleted).map(l => ({
          id: l.id, name: l.name, icon: l.icon, budget: l.budget, owner: l.owner, role: l.role
        })));
      }
      if (method === 'POST') {
        const ledger = {
          id: ++db.seq.ledger,
          name: String(body.name || '未命名账本').slice(0, 128),
          icon: 'fa-book',
          budget: Math.max(0, Number(body.budget) || 0),
          owner: ME,
          role: 'owner',
          members: [{ username: ME, displayName: db.user.displayName, role: 'owner' }],
          transactions: [],
          history: []
        };
        db.ledgers.unshift(ledger);
        persist();
        return delay({ id: ledger.id, name: ledger.name });
      }
    }

    const ledger = findLedger(parts[2]);
    if (!ledger) return fail(404, '演示模式里没有这个账本');

    if (parts.length === 3) {
      if (method === 'PUT') {
        if (body.name !== undefined) ledger.name = String(body.name).slice(0, 128);
        if (body.budget !== undefined) ledger.budget = Math.max(0, Number(body.budget) || 0);
        persist();
        return delay({ ok: true });
      }
      if (method === 'DELETE') {
        ledger.deleted = true;
        ledger.deletedAt = new Date().toISOString();
        persist();
        return delay({ ok: true });
      }
    }

    if (parts[3] === 'transactions') {
      if (method === 'GET') {
        const includeDeleted = query.get('deleted') === '1' && ledger.role === 'owner';
        const rows = ledger.transactions
          .filter(t => includeDeleted || !t.deleted)
          .sort((a, b) => String(b.occurredAt).localeCompare(String(a.occurredAt)));
        return delay(rows);
      }
      if (method === 'POST') {
        const txn = {
          id: ++db.seq.txn,
          type: body.type === 'income' ? 'income' : 'expense',
          amount: Math.abs(Number(body.amount) || 0),
          category: String(body.category || '其他').slice(0, 32),
          note: String(body.note || '').slice(0, 255),
          occurredAt: new Date().toISOString(),
          createdBy: ME,
          paidBy: body.paidBy || ME,
          splitMembers: Array.isArray(body.splitMembers) ? body.splitMembers : [],
          deleted: false
        };
        ledger.transactions.unshift(txn);
        persist();
        return delay(txn);
      }
    }

    if (parts[3] === 'transactions' && parts[4]) {
      const txn = ledger.transactions.find(t => String(t.id) === String(parts[4]));
      if (!txn) return fail(404, '演示模式里没有这笔');
      if (method === 'PUT') {
        if (body.note !== undefined) txn.note = String(body.note).slice(0, 255);
        if (body.category !== undefined) txn.category = String(body.category).slice(0, 32);
        if (body.type !== undefined) txn.type = body.type === 'income' ? 'income' : 'expense';
        if (body.amount !== undefined) txn.amount = Math.abs(Number(body.amount) || 0);
        if (Array.isArray(body.splitMembers)) txn.splitMembers = body.splitMembers;
        persist();
        return delay(txn);
      }
      if (method === 'DELETE') {
        txn.deleted = true;
        ledger.history.unshift({
          id: ++db.seq.history,
          action: 'delete',
          entityType: 'transaction',
          entityId: txn.id,
          snapshot: { ...txn },
          actor: ME,
          createdAt: new Date().toISOString()
        });
        persist();
        return delay({ ok: true });
      }
    }

    if (parts[3] === 'members') {
      if (method === 'GET') return delay({ members: ledger.members.slice(), myRole: ledger.role });
      if (method === 'POST') {
        const username = String(body.username || '').trim();
        const role = ['admin', 'editor', 'viewer'].includes(body.role) ? body.role : 'viewer';
        if (username && !ledger.members.some(m => m.username === username)) {
          ledger.members.push({ username, displayName: username, role });
          persist();
        }
        return delay({ members: ledger.members.slice(), myRole: ledger.role });
      }
      if (method === 'DELETE') {
        const username = String(parts[4] || '');
        ledger.members = ledger.members.filter(m => m.username !== username);
        persist();
        return delay({ members: ledger.members.slice(), myRole: ledger.role });
      }
    }

    if (parts[3] === 'history' && method === 'GET') return delay(ledger.history.slice());

    if (parts[3] === 'restore' && method === 'POST') {
      const entry = ledger.history.find(h => String(h.id) === String(body.historyId));
      if (entry) {
        const txn = ledger.transactions.find(t => String(t.id) === String(entry.entityId));
        if (txn) txn.deleted = false;
      }
      persist();
      return delay({ ok: true });
    }

    if (parts[3] === 'settlement' && method === 'GET') {
      return delay({ transfers: settlement(ledger), start: query.get('start'), end: query.get('end') });
    }

    return fail(404, '演示模式不支持的账本操作');
  }

  // ────────── 对外 ──────────

  window.DemoMode = {
    get enabled() {
      return on;
    },
    enter() {
      db = load() || seed();
      on = true;
      persist();
      return publicMe();
    },
    leave() {
      on = false;
    },
    reset() {
      db = seed();
      persist();
      return publicMe();
    },
    request(path, options) {
      if (!db) db = load() || seed();
      try {
        return Promise.resolve(handle(path, options));
      } catch (error) {
        return fail(500, error.message);
      }
    },
    /** 演示模式的图片上传：直接把文件读成 data URL，不落盘。 */
    async readImage(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error('读取图片失败'));
        reader.readAsDataURL(file);
      });
    }
  };
})();

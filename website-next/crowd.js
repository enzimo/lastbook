/* Lastbook ink crowd: every line is drawn by hand-jittered strokes, redrawn
   about six times a second so the lines "boil" like a pencil cartoon.
   The same code writes the still SVG used when JavaScript is off (node). */
(function () {
  "use strict";
  var INK = "#16150F", WHITE = "#FFFFFF", CREAM = "#FBF5E2", LAMP = "#F6D77A", LAMPD = "#E0A82A",
    WATER = "#9CC3E6", AID = "#C8372D", HATCH = "rgba(22,21,15,.26)", HATCHB = "rgba(22,21,15,.4)", GREY = "#9A978E", PI = Math.PI,
    LINE = "#6B6B66", LINEFAR = "#B2B1AA", SHIRT = "#9C9B94";

  function rng(a) { return function () { a |= 0; a = a + 0x6D2B79F5 | 0; var t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }

  /* ---------- the pen: geometry in local units, jitter, transform ---------- */
  function Pen(out) { this.o = out; this.m = [1, 0, 0, 1, 0, 0]; this.st = []; this.r = Math.random; this.amp = .75; this.lw = 1; }
  Pen.prototype = {
    save: function () { this.st.push([this.m.slice(), this.amp]); },
    restore: function () { var a = this.st.pop(); this.m = a[0]; this.amp = a[1]; },
    mul: function (a, b, c, d, e, f) { var m = this.m; this.m = [m[0] * a + m[2] * b, m[1] * a + m[3] * b, m[0] * c + m[2] * d, m[1] * c + m[3] * d, m[0] * e + m[2] * f + m[4], m[1] * e + m[3] * f + m[5]]; },
    tr: function (x, y) { this.mul(1, 0, 0, 1, x, y); },
    sc: function (x, y) { this.mul(x, 0, 0, y == null ? x : y, 0, 0); },
    rot: function (a) { var c = Math.cos(a), s = Math.sin(a); this.mul(c, s, -s, c, 0, 0); },
    T: function (p) { var m = this.m; return [m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5]]; },
    k: function () { var m = this.m; return Math.sqrt(Math.abs(m[0] * m[3] - m[1] * m[2])); },
    j: function (p, a) { var r = this.r; return [p[0] + (r() - .5) * 2 * a, p[1] + (r() - .5) * 2 * a]; },
    sub: function (pts, close, step) {
      var q = [], n = pts.length, i, a, b, d, s, t;
      for (i = 0; i < (close ? n : n - 1); i++) {
        a = pts[i]; b = pts[(i + 1) % n]; d = Math.hypot(b[0] - a[0], b[1] - a[1]); s = Math.max(1, Math.round(d / step));
        for (t = 0; t < s; t++) q.push([a[0] + (b[0] - a[0]) * t / s, a[1] + (b[1] - a[1]) * t / s]);
      }
      if (!close) q.push(pts[n - 1]);
      return q;
    },
    /* o: {fill, w, col, a, close, step, amp} */
    shape: function (pts, o) {
      var self = this, amp = (o.amp == null ? this.amp : o.amp), q = this.sub(pts, o.close, o.step || 7);
      var jp = q.map(function (p) { return self.j(p, amp); });
      var sp = jp.map(function (p) { return self.T(p); });
      var fc = o.fill, sc = o.col || INK;
      if (this.fade) { if (fc === INK) fc = this.fade; if (sc === INK) sc = this.fade; }
      if (this.soft && fc === CREAM) fc = WHITE;
      if (fc) this.o.fill(sp, fc, o.a == null ? 1 : o.a);
      if (o.w) {
        var sk = sp;
        if (o.close) { sk = sp.concat([this.T(this.j(q[0], amp * 1.6)), this.T(this.j(q[1 % q.length], amp * 1.6))]); }
        this.o.stroke(sk, sc, o.w * this.k() * this.lw * (.88 + this.r() * .24), o.a == null ? 1 : o.a);
      }
    },
    line: function (pts, w, col, a) { this.shape(pts, { w: w || 3, col: col, a: a }); },
    ell: function (cx, cy, rx, ry, o, n, rot) { this.shape(ell(cx, cy, rx, ry, n, rot), mix({ close: 1 }, o)); },
    dot: function (x, y, r, col) { this.shape(ell(x, y, r, r, 8), { close: 1, fill: col || INK, amp: .25 }); },
    glow: function (x, y, r, col, a) { var p = this.T([x, y]); this.o.glow(p[0], p[1], r * this.k(), col, a); },
    shadow: function (x, y, rx, ry, a) { if (this.noShadow) return; var p = this.T([x, y]), k = this.k(); this.o.shadow(p[0], p[1], rx * k, ry * k, a); }
  };
  function mix(a, b) { for (var k in b) a[k] = b[k]; return a; }
  function ell(cx, cy, rx, ry, n, rot) {
    n = n || 22; rot = rot || 0; var p = [], i, t, c = Math.cos(rot), s = Math.sin(rot), x, y;
    for (i = 0; i < n; i++) { t = i / n * 2 * PI - PI * .6; x = rx * Math.cos(t); y = ry * Math.sin(t); p.push([cx + x * c - y * s, cy + x * s + y * c]); }
    return p;
  }
  function rect(x, y, w, h, r) { // rounded rectangle as points
    r = r || 0; var p = [], k, a;
    var cs = [[x + w - r, y + r, -PI / 2], [x + w - r, y + h - r, 0], [x + r, y + h - r, PI / 2], [x + r, y + r, PI]];
    cs.forEach(function (c) { for (k = 0; k <= 3; k++) { a = c[2] + k / 3 * PI / 2; p.push([c[0] + r * Math.cos(a), c[1] + r * Math.sin(a)]); } });
    return p;
  }
  /* pencil shading: parallel "/" strokes in the lower right of an ellipse */
  function hatch(p, cx, cy, rx, ry, n, from, to, w, col) {
    var nx = Math.SQRT1_2, ny = Math.SQRT1_2, R = 1 / Math.sqrt(nx * nx / (rx * rx) + ny * ny / (ry * ry)), i, d, x0, y0, A, B, C, D, s1, s2, ux = Math.SQRT1_2, uy = -Math.SQRT1_2;
    from = from == null ? .38 : from; to = to == null ? .86 : to;
    for (i = 0; i < n; i++) {
      d = R * (from + (to - from) * i / Math.max(1, n - 1)); x0 = d * nx; y0 = d * ny;
      A = ux * ux / (rx * rx) + uy * uy / (ry * ry); B = 2 * (x0 * ux / (rx * rx) + y0 * uy / (ry * ry)); C = x0 * x0 / (rx * rx) + y0 * y0 / (ry * ry) - .86;
      D = B * B - 4 * A * C; if (D <= 0) continue; D = Math.sqrt(D); s1 = (-B - D) / (2 * A) * .8; s2 = (-B + D) / (2 * A) * .8;
      p.shape([[cx + x0 + ux * s1, cy + y0 + uy * s1], [cx + x0 + ux * s2, cy + y0 + uy * s2]], { w: w || 1.4, col: col || HATCH, amp: .5, step: 5 });
    }
  }

  /* ---------- renderers ---------- */
  function smooth(s, p, close) {
    var n = p.length, i, m;
    if (n < 2) return;
    if (close) {
      m = mid(p[n - 1], p[0]); s.M(m[0], m[1]);
      for (i = 0; i < n; i++) { m = mid(p[i], p[(i + 1) % n]); s.Q(p[i][0], p[i][1], m[0], m[1]); }
      s.Z();
    } else {
      s.M(p[0][0], p[0][1]);
      for (i = 1; i < n - 1; i++) { m = mid(p[i], p[i + 1]); s.Q(p[i][0], p[i][1], m[0], m[1]); }
      s.L(p[n - 1][0], p[n - 1][1]);
    }
  }
  function mid(a, b) { return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; }
  function rgba(hex, a) { var n = parseInt(hex.slice(1), 16); return "rgba(" + (n >> 16) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")"; }

  function CanvasOut(ctx) {
    var s = { M: function (x, y) { ctx.moveTo(x, y); }, L: function (x, y) { ctx.lineTo(x, y); }, Q: function (a, b, c, d) { ctx.quadraticCurveTo(a, b, c, d); }, Z: function () { ctx.closePath(); } };
    return {
      fill: function (p, col, a) { ctx.globalAlpha = a; ctx.fillStyle = col; ctx.beginPath(); smooth(s, p, true); ctx.fill(); },
      stroke: function (p, col, w, a) { ctx.globalAlpha = a; ctx.strokeStyle = col; ctx.lineWidth = w; ctx.lineCap = ctx.lineJoin = "round"; ctx.beginPath(); smooth(s, p, false); ctx.stroke(); },
      glow: function (x, y, r, col, a) { var g = ctx.createRadialGradient(x, y, 0, x, y, r); g.addColorStop(0, rgba(col, a)); g.addColorStop(.45, rgba(col, a * .55)); g.addColorStop(1, rgba(col, 0)); ctx.globalAlpha = 1; ctx.fillStyle = g; ctx.fillRect(x - r, y - r, 2 * r, 2 * r); },
      shadow: function (x, y, rx, ry, a) { ctx.save(); ctx.translate(x, y); ctx.scale(1, ry / rx); var g = ctx.createRadialGradient(0, 0, 0, 0, 0, rx); g.addColorStop(0, "rgba(22,21,15," + a + ")"); g.addColorStop(.55, "rgba(22,21,15," + a * .75 + ")"); g.addColorStop(1, "rgba(22,21,15,0)"); ctx.globalAlpha = 1; ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, rx, 0, 2 * PI); ctx.fill(); ctx.restore(); }
    };
  }
  function SvgOut() {
    var parts = [], defs = [], id = 0;
    function f(n) { return Math.round(n * 10) / 10; }
    function d(p, close) { var o = ""; smooth({ M: function (x, y) { o += "M" + f(x) + " " + f(y); }, L: function (x, y) { o += "L" + f(x) + " " + f(y); }, Q: function (a, b, c, e) { o += "Q" + f(a) + " " + f(b) + " " + f(c) + " " + f(e); }, Z: function () { o += "Z"; } }, p, close); return o; }
    return {
      parts: parts, defs: defs,
      fill: function (p, col, a) { parts.push('<path d="' + d(p, true) + '" fill="' + col + '"' + (a < 1 ? ' fill-opacity="' + a + '"' : "") + "/>"); },
      stroke: function (p, col, w, a) { parts.push('<path d="' + d(p, false) + '" fill="none" stroke="' + col + '" stroke-width="' + f(w) + '"' + (a < 1 ? ' stroke-opacity="' + a + '"' : "") + "/>"); },
      glow: function (x, y, r, col, a) { var g = "g" + (id++); defs.push('<radialGradient id="' + g + '"><stop offset="0" stop-color="' + col + '" stop-opacity="' + a + '"/><stop offset=".45" stop-color="' + col + '" stop-opacity="' + a * .55 + '"/><stop offset="1" stop-color="' + col + '" stop-opacity="0"/></radialGradient>'); parts.push('<circle cx="' + f(x) + '" cy="' + f(y) + '" r="' + f(r) + '" fill="url(#' + g + ')"/>'); },
      shadow: function (x, y, rx, ry, a) { var g = "s" + (id++); defs.push('<radialGradient id="' + g + '"><stop offset="0" stop-color="#16150F" stop-opacity="' + a + '"/><stop offset=".55" stop-color="#16150F" stop-opacity="' + a * .75 + '"/><stop offset="1" stop-color="#16150F" stop-opacity="0"/></radialGradient>'); parts.push('<ellipse cx="' + f(x) + '" cy="' + f(y) + '" rx="' + f(rx) + '" ry="' + f(ry) + '" fill="url(#' + g + ')"/>'); },
      raw: function (s) { parts.push(s); }
    };
  }

  /* ---------- the people ---------- */
  // Local units: feet at (0,0), up is negative y. About 68 units tall.
  function legs(p, st, sit) {
    if (sit) return;
    var f = st.walk ? Math.floor(st.t / .22) % 2 : -1, L, R;
    if (f === 0) { L = [-9, 0]; R = [8, 0]; } else if (f === 1) { L = [-2, -2.5]; R = [3, 0]; } else { L = [-6, 0]; R = [6, 0]; }
    p.line([[-5, -15], [(L[0] - 5) / 2, -7], L], 3.2);
    p.line([[5, -15], [(R[0] + 5) / 2, -7], R], 3.2);
    p.ell(L[0] + 1.6, L[1] - 1.2, 4.6, 2.7, { fill: INK, amp: .3 }, 10);
    p.ell(R[0] + 1.6, R[1] - 1.2, 4.6, 2.7, { fill: INK, amp: .3 }, 10);
  }
  function body(p, F, by, rx, ry) {
    var pts = [], i, t, s, c, k;
    for (i = 0; i < 22; i++) { t = i / 22 * 2 * PI; s = Math.sin(t); c = Math.cos(t); k = F.big ? (s > 0 ? 1 + .06 * s : 1 - .02 * -s) : (s > 0 ? 1 + .12 * s : 1 - .1 * -s); pts.push([rx * c * k, by + ry * s]); }
    p.shape(pts, { close: 1, fill: F.shirt || F.fill, w: 3 });
    if (F.big) hatch(p, 0, by, rx, ry, 6, .3, .9, 1.25, HATCHB); else hatch(p, 0, by, rx, ry, 4);
  }
  function head(p, F, hx, hy, look) {
    p.ell(hx, hy, 11.5, 11, { fill: F.fill, w: 3 });
    if (F.big) hatch(p, hx, hy, 11.5, 11, 3, .45, .85, 1.1, HATCHB); else hatch(p, hx, hy, 11.5, 11, 2, .55, .8);
    var lx = look[0], ly = look[1];
    if (!F.noFace) {
      p.dot(hx - 4 + lx, hy - 1.5 + ly * .3, 1.4);
      p.dot(hx + 3.6 + lx, hy - 1.5 + ly * .3, 1.4);
      var mx = hx + lx * .6, my = hy + 3.4 + ly * .3;
      p.line([[mx - 4, my], [mx - 2.5, my + 2.5], [mx, my + 3.5], [mx + 2.5, my + 2.5], [mx + 4, my]], 1.7);
    }
    hat(p, F.hat, hx, hy);
  }
  function hat(p, h, x, y) {
    if (!h) return;
    if (h === "cap") { p.shape([[x - 12, y - 3], [x - 11, y - 9], [x - 4, y - 13.5], [x + 5, y - 13], [x + 11, y - 7], [x + 18, y - 4.5], [x + 11, y - 2.5]], { close: 1, fill: INK, w: 2.4 }); }
    else if (h === "beanie") { p.shape([[x - 12, y - 2], [x - 10, y - 10], [x - 2, y - 14], [x + 7, y - 12.5], [x + 12, y - 4], [x + 12, y - 1.5]], { close: 1, fill: INK, w: 2.4 }); p.dot(x - 1, y - 16, 3.2); p.line([[x - 11, y - 3.5], [x + 11, y - 3]], 2.2, WHITE); }
    else if (h === "scarf") { p.shape([[x - 13, y + 5], [x - 12.5, y - 7], [x - 3, y - 14], [x + 8, y - 12], [x + 13, y - 3], [x + 12, y + 4], [x + 9, y - 4], [x, y - 8], [x - 9, y - 3], [x - 10, y + 6]], { close: 1, fill: WHITE, w: 2.6 }); p.dot(x - 4, y - 8, 1.1, HATCH); p.dot(x + 4, y - 9, 1.1, HATCH); p.dot(x + 9, y - 5, 1.1, HATCH); p.shape([[x - 12, y + 4], [x - 17, y + 10], [x - 12, y + 11]], { w: 2.4, fill: WHITE }); }
    else if (h === "straw") { p.ell(x + 1, y - 6, 21, 5, { fill: CREAM, w: 2.6 }, 24); p.shape([[x - 9, y - 7], [x - 8, y - 15], [x, y - 18], [x + 8, y - 16], [x + 10, y - 7]], { fill: CREAM, w: 2.6 }); p.line([[x - 8.5, y - 9.5], [x + 9.5, y - 9.5]], 2.2); }
  }
  function arm(p, pts) { p.line(pts, 3); }

  /* props: all at the figure's front (+x) */
  function book(p, x, y, s, turn) {
    p.save(); p.tr(x, y); p.sc(s);
    p.shape([[0, -1], [-6, -4], [-13, -3.5], [-14, 4], [-7, 3], [0, 6]], { close: 1, fill: WHITE, w: 2.4 });
    p.shape([[0, -1], [6, -4], [13, -3.5], [14, 4], [7, 3], [0, 6]], { close: 1, fill: WHITE, w: 2.4 });
    p.line([[-10.5, -.5], [-3, .2]], 1.3, HATCH); p.line([[-10.5, 2], [-3, 2.6]], 1.3, HATCH);
    p.line([[3, .2], [10.5, -.5]], 1.3, HATCH); p.line([[3, 2.6], [10.5, 2]], 1.3, HATCH);
    if (turn > 0) { var a = turn * PI; p.line([[0, -1], [Math.cos(a) * 12, -1 - Math.sin(a) * 9], [Math.cos(a) * 13, 4 - Math.sin(a) * 6]], 2.2); }
    p.restore();
  }
  var DRAW = {
    reader: function (p, F, st) { // sitting cross-legged, book on lap
      p.shadow(10 * F.dir, 1, 22, 6, .14);
      p.ell(-7, -5, 10, 5.2, { fill: F.fill, w: 3 }, 16, .1); p.ell(7, -5, 10, 5.2, { fill: F.fill, w: 3 }, 16, -.1);
      body(p, F, -21, 12.5, 14);
      head(p, F, 2, -44, [1, 3.6]);
      book(p, 2, -12, 1.05, st.turn);
      arm(p, [[-10, -27], [-14, -18], [-11, -12]]); arm(p, [[10, -27], [15, -19], [15, -12]]);
    },
    laptop: function (p, F, st) {
      p.shadow(10 * F.dir, 1, 22, 6, .14);
      p.ell(-7, -5, 10, 5.2, { fill: F.fill, w: 3 }, 16, .1); p.ell(7, -5, 10, 5.2, { fill: F.fill, w: 3 }, 16, -.1);
      body(p, F, -21, 12.5, 14);
      head(p, F, 1, -44, [2.5, 3.2]);
      // laptop: screen tilted up, tiny eye-O logo
      p.shape([[2, -11], [26, -11], [27, -8], [1, -8]], { close: 1, fill: WHITE, w: 2.4 });
      p.shape([[6, -11], [9, -31], [29, -30], [26, -11]], { close: 1, fill: WHITE, w: 2.6 });
      p.ell(17.5, -21, 3.8, 3.8, { w: 2 }, 10); p.dot(17.5, -19.8, 1.6);
      p.glow(17.5, -21, 14, LAMP, .45);
      arm(p, [[-10, -27], [-8, -15], [3, -10]]); arm(p, [[10, -27], [12, -16], [8, -10]]);
    },
    lantern: function (p, F, st) {
      p.glow(18, -16, 30, LAMP, .55);
      p.shadow(10 * F.dir, 1, 17, 5.5, .14);
      legs(p, st); body(p, F, -29, 13, 16);
      arm(p, [[-10, -36], [-14, -27], [-13, -20]]);
      head(p, F, 1, -55, [2.5, 2]);
      arm(p, [[10, -37], [17, -33], [19, -31]]);
      // lantern hanging from the hand
      p.line([[14, -23], [17, -31], [23, -30], [24, -23]], 1.8);
      p.shape(rect(12.5, -23, 13, 3, 1), { close: 1, fill: INK, amp: .3 });
      p.shape(rect(13.5, -20.5, 11, 12, 2), { close: 1, fill: LAMP, w: 2.2 });
      p.shape([[19, -18], [21.5, -13], [19, -10.5], [16.5, -13]], { close: 1, fill: LAMPD, amp: .4 });
      p.line([[16.5, -20], [16.5, -9]], 1.4); p.line([[21.5, -20], [21.5, -9]], 1.4);
      p.shape(rect(12, -9, 14, 3, 1), { close: 1, fill: INK, amp: .3 });
    },
    jug: function (p, F, st) {
      p.shadow(10 * F.dir, 1, 19, 6, .14);
      p.save(); p.rot(-.07);
      legs(p, st); body(p, F, -29, 13.5, 16);
      head(p, F, -1, -55, [2.5, 2.8]);
      arm(p, [[-10, -36], [-15, -28], [-12, -21]]);
      // jug at the side
      p.shape(rect(11, -26, 15, 18, 5), { close: 1, fill: WHITE, w: 2.6 });
      p.shape([[12.5, -18], [17, -19], [21, -17.5], [24.5, -18.5], [24.5, -12], [22, -9.5], [14, -9.5], [12.5, -12]], { close: 1, fill: WATER, amp: .4 });
      p.shape(rect(11, -26, 15, 18, 5), { close: 1, w: 2.6 });
      p.shape(rect(14.5, -30, 7, 4.5, 1.5), { close: 1, fill: WHITE, w: 2.2 });
      p.line([[26, -23], [30, -21], [29.5, -14], [26, -12.5]], 2.2);
      arm(p, [[10, -38], [14, -33], [16, -29]]);
      p.restore();
    },
    aid: function (p, F, st) {
      p.shadow(10 * F.dir, 1, 18, 6, .14);
      legs(p, st); body(p, F, -29, 13, 16);
      head(p, F, 1, -55, [2.2, 3]);
      arm(p, [[-10, -37], [-8, -28], [2, -24]]);
      p.shape(rect(3, -34, 22, 15, 3), { close: 1, fill: WHITE, w: 2.6 });
      p.line([[10, -34], [10.5, -37.5], [17.5, -37.5], [18, -34]], 2.2);
      p.shape(rect(12, -30.5, 4.6, 8.6, .6), { close: 1, fill: AID, amp: .25 });
      p.shape(rect(9.7, -28.3, 9.2, 4.4, .6), { close: 1, fill: AID, amp: .25 });
      arm(p, [[10, -37], [18, -31], [21, -25]]);
    },
    phone: function (p, F, st) {
      p.shadow(10 * F.dir, 1, 17, 5.5, .14);
      legs(p, st); body(p, F, -29, 13, 16);
      arm(p, [[-10, -36], [-14, -27], [-12, -20]]);
      head(p, F, 0, -55, [2.6, -3]);
      arm(p, [[9, -40], [16, -52], [17, -64]]);
      p.save(); p.tr(17, -70); p.rot(.15);
      p.shape(rect(-4, -7, 8, 13, 2), { close: 1, fill: INK, amp: .3 });
      p.shape(rect(-2.6, -5.4, 5.2, 9, 1), { close: 1, fill: WHITE, amp: .2 });
      p.restore();
      // empty signal bars and a little cross
      p.line([[25, -80], [29, -76]], 1.8); p.line([[29, -80], [25, -76]], 1.8);
    },
    radio: function (p, F, st) {
      p.shadow(10 * F.dir, 1, 18, 6, .14);
      legs(p, st); body(p, F, -29, 13, 16);
      head(p, F, 0, -55, [2.4, 2.4]);
      p.line([[6, -34], [14, -50], [17, -56]], 1.8); p.dot(17.4, -56.6, 2);
      p.shape(rect(2, -35, 22, 14, 3), { close: 1, fill: CREAM, w: 2.6 });
      p.ell(9, -28, 4.2, 4.2, { fill: WHITE, w: 2 }, 12);
      p.line([[7, -29], [11, -27]], 1.1, HATCH); p.line([[7, -27], [11, -29]], 1.1, HATCH);
      p.line([[16, -31], [21, -31]], 1.8); p.line([[16, -27], [21, -27]], 1.8); p.dot(18.5, -24, 1.4);
      arm(p, [[-10, -36], [-6, -27], [2, -26]]); arm(p, [[10, -37], [22, -33], [24, -27]]);
    },
    garden: function (p, F, st) {
      p.shadow(10 * F.dir, 1, 17, 5.5, .14);
      legs(p, st); body(p, F, -29, 13, 16);
      arm(p, [[-10, -36], [-14, -27], [-12, -20]]);
      head(p, F, 0, -55, [2.8, 1.6]);
      arm(p, [[10, -38], [16, -36], [18, -42]]);
      p.save(); p.tr(20, -48); p.rot(.18);
      p.shape(rect(-5, -7, 10, 13, 1.5), { close: 1, fill: WHITE, w: 2.2 });
      p.line([[0, 3], [0, -2]], 1.6, "#2F6B4F"); p.ell(-2, -2.5, 2.2, 1.3, { fill: "#2F6B4F", amp: .2 }, 8, -.5); p.ell(2, -3.5, 2.2, 1.3, { fill: "#2F6B4F", amp: .2 }, 8, .5);
      p.restore();
    },
    kid: function (p, F, st) {
      p.shadow(9 * F.dir, 1, 15, 5, .14);
      legs(p, st);
      p.shape(rect(-21, -40, 10, 18, 3.5), { close: 1, fill: CREAM, w: 2.6 }); p.line([[-20, -33], [-12, -33]], 1.8);
      body(p, F, -29, 12, 15);
      head(p, F, 1, -54, [-2.4, 2.6]);
      arm(p, [[-9, -37], [-12, -29], [-10, -21]]); arm(p, [[9, -37], [13, -29], [11, -21]]);
      p.line([[-9, -40], [-4, -24]], 1.8);
    },
    extra: function (p, F, st) {
      p.shadow(10 * F.dir, 1, 17, 5.5, .14);
      legs(p, st); body(p, F, -29 - st.bob, 13, 16);
      var sw = st.walk ? (Math.floor(st.t / .22) % 2 ? 3 : -3) : 0;
      arm(p, [[-10, -36 - st.bob], [-13 - sw, -28], [-11 - sw * 1.5, -21]]);
      head(p, F, 1, -55 - st.bob, F.look || [2.4, .6]);
      arm(p, [[10, -36 - st.bob], [13 + sw, -28], [11 + sw * 1.5, -21]]);
    }
  };

  /* ---------- the giant lantern behind the wordmark ---------- */
  function giant(p, x, y, s, swing, flick, top) {
    var w = 2.5 / s;
    p.save(); p.tr(x, y); p.rot(swing); p.sc(s);
    p.amp = 1.3;
    var cord = top == null ? -1200 : Math.min(0, (top - y) / s);
    p.line([[0, cord], [0, cord / 2], [0, 0]], w * 1.2, INK, .8);
    p.ell(0, 8, 8, 8, { w: w * 1.4, a: .85 }, 14);
    p.line([[-50, 78], [-42, 34], [-18, 16], [0, 14], [18, 16], [42, 34], [50, 78]], w * 1.4, INK, .85);
    // cap
    p.shape([[-58, 82], [-48, 60], [-24, 46], [0, 42], [24, 46], [48, 60], [58, 82]], { close: 1, fill: WHITE, w: w * 1.5, a: 1 });
    hatch(p, 20, 66, 34, 14, 5, .1, .9, w);
    p.ell(-16, 66, 4, 3, { w: w }, 8); p.ell(0, 63, 4, 3, { w: w }, 8); p.ell(16, 66, 4, 3, { w: w }, 8);
    p.shape(rect(-62, 82, 124, 14, 4), { close: 1, fill: WHITE, w: w * 1.5 });
    // glass and glow
    p.glow(0, 190, 300, LAMP, .36 + flick * .05);
    p.glow(0, 168, 90, LAMP, .5 + flick * .08);
    p.glow(0, 176, 46, LAMPD, .35 + flick * .1);
    p.shape([[-50, 96], [-56, 150], [-54, 200], [-48, 236], [48, 236], [54, 200], [56, 150], [50, 96]], { close: 1, w: w * 1.4, a: .9 });
    p.line([[-18, 98], [-21, 165], [-18, 234]], w, INK, .8); p.line([[18, 98], [21, 165], [18, 234]], w, INK, .8);
    for (var i = 0; i < 7; i++) p.line([[30 + i * 3.2, 110 + i * 3], [36 + i * 2.6, 140 + i * 6]], w * .8, HATCH);
    // flame
    var fh = 50 + flick * 4;
    p.shape([[0, 222], [-14, 206], [-12, 190], [-3, 222 - fh * .9], [0, 222 - fh], [5, 222 - fh * .7], [13, 196], [12, 210]], { close: 1, fill: LAMP, w: w * 1.2, a: 1 });
    p.shape([[0, 220], [-6, 210], [-4, 198], [0, 188], [5, 200], [6, 212]], { close: 1, fill: LAMPD, amp: .7 });
    p.line([[0, 222], [0, 234]], w * 1.6);
    // base tank
    p.shape(rect(-64, 236, 128, 14, 4), { close: 1, fill: WHITE, w: w * 1.5 });
    p.shape([[-60, 250], [-66, 274], [-58, 296], [-30, 306], [30, 306], [58, 296], [66, 274], [60, 250]], { close: 1, fill: WHITE, w: w * 1.5 });
    hatch(p, 26, 278, 40, 26, 7, .05, .95, w);
    p.restore();
  }

  /* ---------- cast and layout ---------- */
  // shirt: a grey top, like a printed T-shirt. All the crowd is white with soft grey lines.
  var CAST = {
    reader: { kind: "reader" }, kid: { kind: "kid", shirt: 1 }, lantern: { kind: "lantern", hat: "beanie", shirt: 1 },
    garden: { kind: "garden", hat: "straw" }, aid: { kind: "aid", shirt: 1 }, jug: { kind: "jug", hat: "scarf" },
    phone: { kind: "phone", mouthO: 1, shirt: 1 }, radio: { kind: "radio", hat: "cap", mouthO: 1 }, laptop: { kind: "laptop", shirt: 1 },
    big1: { kind: "reader", hat: "scarf", shirt: 1 },
    x1: { kind: "extra", hat: "cap", walk: 1 }, x2: { kind: "extra", walk: 1, shirt: 1 }, x3: { kind: "extra", hat: "beanie", walk: 1 }, x4: { kind: "extra", walk: 1, look: [-1, -2] }
  };
  // desktop: [who, across (0 = left edge, 1 = right), down to the feet (0 = top, 1 = bottom)].
  // Size comes from the depth: small and far at the top, big and near at the bottom.
  var DESK = [
    ["x2", .44, .55], ["x4", .585, .53], ["x3", .37, .60],
    ["phone", .15, .57], ["radio", .82, .55], ["x1", .90, .65],
    ["lantern", .25, .68], ["jug", .74, .68],
    ["kid", .07, .76], ["garden", .94, .79],
    ["aid", .175, .86], ["big1", .06, .97], ["reader", .89, .96]
  ];
  // phone: at most two people, never on the words. [who, across, where the feet are, size]:
  // "floor" is the bottom of the hero, "side" is beside the wordmark's last line.
  // Each one slides out towards its edge until it is clear; if it can't be, it isn't drawn.
  var PHONE = [["lantern", .84, "floor", 1.1], ["phone", .14, "floor", 1], ["reader", .08, "side", .85], ["jug", .93, "side", .85]];
  var PHONE_MAX = 2;
  function boxOf(x, y, s) { return [x - 26 * s, y - 82 * s, x + 32 * s, y + 4]; }
  function hits(bx, R) { for (var i = 0; i < R.length; i++) { var r = R[i]; if (bx[0] < r[2] && bx[2] > r[0] && bx[1] < r[3] && bx[3] > r[1]) return r; } return null; }
  function hitsFig(bx, figs, me) { for (var i = 0; i < figs.length; i++) { var o = figs[i]; if (o !== me && !(o.far && me && !me.far) && !(me && me.far && !o.far)) { var b = boxOf(o.x, o.y, o.s); if (bx[0] < b[2] + 6 && bx[2] + 6 > b[0] && bx[1] < b[3] && bx[3] > b[1]) return o; } } return null; }
  function depthOf(v) { return Math.max(0, Math.min(1, (v - .3) / .64)); }
  function layout(W, H, B, phone) {
    var out = [], list = phone ? PHONE : DESK, i, e, c, x, y, s, d, far, R, k, n, bx, F;
    // overall size: by the height, and narrower windows get smaller people
    // on a tall window (a tablet held upright) the crowd gathers round the words instead of spreading to the top and bottom
    var span = Math.min(H, W * .75), cy = span < H ? B.cy : H / 2;
    var K = Math.max(.6, Math.min(1.12, span / 900, W / 1300));
    for (i = 0; i < list.length; i++) {
      e = list[i]; c = CAST[e[0]];
      if (phone) {
        if (out.length >= PHONE_MAX) break;
        x = W * e[1]; y = e[2] === "floor" ? H - 10 : B.wm[1] - 4; s = e[3]; far = false; d = .8;
        // slide towards the near edge until clear of the words, the buttons and the other person
        for (n = 0; n < 30; n++) { bx = boxOf(x, y, s); if (!hits(bx, B.rects) && !hitsFig(bx, out, { far: false }) && bx[1] > B.top) break; x += x < W / 2 ? -6 : 6; }
        // at most a third of a person may leave the screen
        if (n === 30 || x < 10 * s || x > W - 10 * s) continue;
      } else {
        x = W * e[1]; y = cy + span * (e[2] - .5); d = depthOf(e[2]); s = (1 + 1.7 * d) * K; far = d < .2;
        if (y > H - 4) y = H - 4;
        R = far ? B.text : B.rects;
        // step out from the middle until clear of the words and the other people
        for (n = 0; n < 40; n++) { bx = boxOf(x, y, s); if (!hits(bx, R) && !hitsFig(bx, out, { far: far })) break; x += x < B.cx ? -14 : 14; }
        if (n === 40 || x < 20 || x > W - 20) continue;
      }
      F = { id: e[0], kind: c.kind, hat: c.hat, mouthO: c.mouthO, look: c.look, fill: WHITE, shirt: c.shirt ? SHIRT : null,
        walk: !!c.walk, far: far, d: d, x: x, y: y, s: s, dir: (i % 2 ? -1 : 1), seed: 11 + i * 37,
        speed: (8 + (i * 7) % 7) * s, ph: i * 1.7, t: 0 };
      // walkers pace a stretch of floor that is free of words and people
      if (F.walk) {
        R = far ? B.text : B.rects;
        for (k = 0; k < 2; k++) {
          var lim = k ? W - 20 : 20, step = k ? 6 : -6, xx = x;
          while ((k ? xx < lim : xx > lim) && Math.abs(xx - x) < (phone ? 60 : 160) && !hits(boxOf(xx + step, y, s), R) && !hitsFig(boxOf(xx + step, y, s), out, F)) xx += step;
          if (k) F.hi = xx; else F.lo = xx;
        }
      }
      out.push(F);
    }
    return out;
  }

  function mixHex(a, b, t) { var x = parseInt(a.slice(1), 16), y = parseInt(b.slice(1), 16), r = function (sh) { return Math.round(((x >> sh) & 255) * (1 - t) + ((y >> sh) & 255) * t); }; return 'rgb(' + r(16) + ',' + r(8) + ',' + r(0) + ')'; }
  /* Distant skyline, impact and blackout, drawn with the crowd's ink pen. */
  function disasterSky(p, W, H, G, now, still) {
    if (!G || W < 760) return;
    var top = G.top + 18;
    // Keep room for the skyline when the hero uses a compact layout.
    var floor = Math.max(top + 100, Math.min(H * .29, G.y + 65));
    var height = floor - top;
    var t = still ? 12 : (now / 1000) % 26;
    var meteorPaths = [[.43,.23],[.57,.81],[.04,.29],[.96,.67],[.31,.12]];
    var path = meteorPaths[still ? 0 : Math.floor(now / 26000) % meteorPaths.length];
    var fade = t > 23 ? (26 - t) / 3 : 1;
    var scale = Math.min(1, W / 1200);
    var buildings = [
      [.06, 48, 58], [.12, 40, 91], [.17, 56, 73], [.23, 45, 120],
      [.29, 60, 86], [.36, 38, 58], [.61, 48, 72], [.67, 46, 110],
      [.73, 64, 89], [.81, 42, 134], [.87, 53, 69], [.93, 34, 51]
    ];
    p.save(); p.fade = null; p.soft = 0; p.amp = .9;
    p.r = rng(3107 + Math.floor(now / 180) % 3);
    p.line([[W * .025, floor], [W * .48, floor + 2], [W * .975, floor]], 1.5, '#99968C', .45);
    // A quieter, overlapping far skyline gives the nearer roofs depth.
    buildings.forEach(function (b, index) {
      var x = W * b[0] - 18 * scale, w = b[1] * scale * 1.3;
      var h = Math.min((b[2] + 28) * scale, height * .72);
      p.shape([[x-w/2,floor],[x-w/2,floor-h],[x-w*.12,floor-h],
        [x-w*.12,floor-h-12*scale],[x+w*.2,floor-h-12*scale],
        [x+w*.2,floor-h],[x+w/2,floor-h],[x+w/2,floor]],
        {close:1,fill:'#C8C4BA',col:'#B7B3AA',w:1,a:.42});
    });
    buildings.forEach(function (b, index) {
      var x = W * b[0], w = b[1] * scale * 1.14;
      var h = Math.min(b[2] * scale, height * .72), y = floor - h;
      var roof = index % 3;
      var outline = roof === 0 ? [[x-w/2,floor],[x-w/2,y+13*scale],
        [x,y],[x+w/2,y+13*scale],[x+w/2,floor]] :
        [[x-w/2,floor],[x-w/2,y],[x+w/2,y],[x+w/2,floor]];
      p.shape(outline,{close:1,fill:'#E1DDD2',col:'#77756D',w:1.8,a:.88});
      p.shape([[x+w*.20,floor],[x+w*.20,y+5],[x+w/2,y+5],[x+w/2,floor]],
        {close:1,fill:'#B9B4A9',a:.32});
      if (roof === 1) {
        p.line([[x-w/2-2,y-5],[x+w/2+2,y-5]],1.4,'#77756D',.7);
        p.line([[x-w/2,y+h*.32],[x+w/2,y+h*.32]],1,'#9C978D',.5);
      }
      if (index === 3 || index === 9) {
        p.shape(rect(x-w*.12,y-10*scale,w*.24,10*scale,1),
          {close:1,fill:'#BBB6AC',col:'#77756D',w:1.3,a:.85});
        p.line([[x,y-10*scale],[x,y-25*scale]],1.2,'#77756D',.7);
        p.line([[x-7*scale,y-20*scale],[x+7*scale,y-20*scale]],1,'#77756D',.7);
      }
      var lights = t < 10 + index * .18 || t > 24.5;
      var rows = Math.floor((h - 24 * scale) / (16 * scale));
      for (var row = 0; row < rows; row++) {
        for (var col = 0; col < 3; col++) {
          p.shape(rect(x-w*.34+col*w*.25,y+19*scale+row*16*scale,w*.13,6*scale,.5),
            {close:1,fill:lights ? '#DFA942' : '#7A7770',a:lights ? .85*fade : .45});
        }
      }
      p.shape(rect(x-w*.09,floor-13*scale,w*.18,13*scale,1),
        {close:1,fill:'#969187',a:.65});
    });
    // Cycle through five approaches, with each impact following its meteor.
    if (t >= 3 && t < 8) {
      var q = (t - 3) / 5;
      var mx = W * (path[0] + q * (path[1] - path[0])), my = top + q * height;
      var tailX = W * (path[0] - path[1]), tailY = -height;
      var tailLength = Math.sqrt(tailX * tailX + tailY * tailY);
      tailX = tailX / tailLength * 175 * scale;
      tailY = tailY / tailLength * 175 * scale;
      var normalX = -tailY / (175 * scale), normalY = tailX / (175 * scale);
      var rotation = Math.atan2(-tailX, tailY);
      p.glow(mx, my, 47 * scale, '#E7A446', .25);
      p.shape([[mx + tailX, my + tailY],
        [mx + normalX * 8, my + normalY * 8], [mx - normalX * 8, my - normalY * 8]],
        {close:1, fill:'#D99047', a:.42});
      p.line([[mx + tailX, my + tailY], [mx + tailX * .16, my + tailY * .16], [mx, my]],
        2.8, '#C2783E', .7);
      p.ell(mx,my,14*scale,17*scale,{fill:'#D47D3F',col:'#BA6936',w:1.6},9,rotation);
      p.ell(mx-2*scale,my+2*scale,10*scale,12*scale,{fill:'#625A4E',col:'#514B42',w:1.6},8,rotation);
      p.line([[mx+tailX*.74,my+tailY*.74],[mx+tailX*.23,my+tailY*.23],
        [mx+tailX*.05,my+tailY*.05]],4,'#E9B254',.75);
    }
    if (t >= 8 && t < 14) {
      var impact = (t - 8) / 6, ix = W * path[1];
      var spread = Math.sin(Math.min(1, impact * 2) * PI / 2);
      var blast = Math.min(W * .20, height * .82) * spread;
      var opacity = Math.min(1,impact*15) * (1-impact);
      // A loose ink burst and ground ripple keep the impact in the sketch style.
      p.ell(ix,floor,blast*1.15,blast*.12,{col:'#918A7B',w:1.5,a:opacity*.55},28);
      var burst = [];
      for (var ray = 0; ray <= 18; ray++) {
        var angle = PI + ray * PI / 18;
        var radius = blast * (ray % 2 ? .46 : .67);
        burst.push([ix + Math.cos(angle) * radius,
          floor + Math.sin(angle) * radius * .8]);
      }
      p.line(burst,1.8,'#8E7960',opacity*.8);
      if (impact < .22) {
        p.ell(ix,floor-8*scale,blast*.32,blast*.2,
          {fill:'#E4BA70',a:opacity*.24},13);
      }
      for (var k = 0; k < 13; k++) {
        var debrisAngle = PI * (1.05 + k * .075);
        var distance = impact * 210 * scale;
        var dx = ix + Math.cos(debrisAngle) * distance;
        var dy = floor + Math.sin(debrisAngle) * distance + impact*impact*90*scale;
        p.line([[dx,dy],[dx-Math.cos(debrisAngle)*11*scale,dy-Math.sin(debrisAngle)*11*scale]],
          2,'#8D7760',opacity*.8);
        p.ell(dx,dy,2.4*scale,1.8*scale,{fill:'#786A58',a:opacity},7);
      }
      p.line([[ix-blast*.85,floor+4],[ix-blast*.55,floor-4],
        [ix-blast*.28,floor+1],[ix,floor-5],[ix+blast*.35,floor+2],
        [ix+blast*.7,floor-3],[ix+blast*.95,floor+4]],1.2,'#A49A88',opacity*.55);
    }
    if (t >= 8 && t < 24) {
      [3, 8].forEach(function (index) {
        var b = buildings[index], x = W * b[0], h = Math.min(b[2] * scale, height * .66);
        var y = floor - h + 9;
        var strength = Math.min(1, (t - 8) / 2) * fade;
        p.glow(x, y, 38 * scale, '#D68F41', .15 * strength);
        for (var smoke = 0; smoke < 6; smoke++) {
          var rise = ((t * 18 + smoke * 24) % 115) * scale;
          p.ell(x + Math.sin(t + smoke) * 9 * scale, y - rise, (14 + rise * .20) * scale,
            (16 + rise * .22) * scale, {fill:'#827D72', a:(1 - rise / 135) * .26 * strength}, 15);
        }
        for (var flame = 0; flame < 3; flame++) {
          var fx = x + (flame - 1) * 9 * scale;
          var fh = (34 + Math.sin(t * 6 + flame * 2) * 10) * scale * strength;
          p.shape([[fx - 7 * scale,y + 10], [fx - 5 * scale,y - fh * .3],
            [fx + 3 * scale,y - fh], [fx + 2 * scale,y - fh * .2], [fx + 7 * scale,y + 10]],
            {close:1, fill:flame === 1 ? '#E8B34F' : '#CF8150', a:.7 * strength});
        }
      });
    }
    p.restore(); p.soft = 1; p.fade = LINE;
  }

  function scene(p, figs, G, now, frame, still, H, W) {
    p.soft = 1; p.fade = LINE;
    disasterSky(p, W, H, G, now, still);
    if (G) { p.r = rng(5 + frame * 977); giant(p, G.x, G.y, G.s, still ? 0 : Math.sin(now / 1000 * 2 * PI / 7) * .02, still ? .5 : Math.sin(now / 90) * .5 + Math.sin(now / 37) * .3, G.top); }
    var order = figs.slice().sort(function (a, b) { return a.y - b.y; });
    // long soft shadows on the floor first, so none falls over a person in front
    order.forEach(function (F) { p.o.shadow(F.x + 26 * F.s, F.y, 44 * F.s, 6.5 * F.s, .1); });
    p.noShadow = 1;
    order.forEach(function (F) {
      p.r = rng(F.seed * 31 + frame * 977);
      p.fade = mixHex(LINE, LINEFAR, (1 - F.d) * .8);
      p.lw = (2.2 + .8 * F.s) / (3 * F.s);
      p.amp = .75 * Math.min(1, 1.4 / F.s);
      var t = now / 1000 + F.ph;
      var st = { t: t, walk: F.walk && !still, bob: 0, turn: 0 };
      if (st.walk) st.bob = Math.floor(t / .22) % 2 ? 1.6 : 0;
      if (F.kind === "reader" && !still) { var q = (t % 4) / .45; st.turn = q < 1 ? q : 0; }
      p.save(); p.tr(F.x, F.y); p.sc(F.s);
      if (!F.walk && !still) p.rot(Math.sin(t * 2 * PI / 3) * .022);
      if (F.dir < 0) p.sc(-1, 1);
      DRAW[F.kind](p, F, st);
      p.restore();
    });
    p.lw = 1; p.amp = .75; p.noShadow = 0; p.fade = null; p.soft = 0;
  }
  function fixFacing(figs, W) { figs.forEach(function (F) { if (!F.walk) F.dir = F.x < W / 2 ? 1 : -1; }); }
  function bubY(F) { return F.y - (F.kind === "reader" || F.kind === "laptop" ? 56 : F.kind === "phone" ? 98 : 80) * F.s; }
  var CALM = [["lantern", "generator works!"], ["jug", "found clean water!"], ["aid", "preserved our first harvest!", 1]];

  /* ---------- node: the still SVGs for JavaScript off, and the drawing kit for tools/art ---------- */
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      still: function (W, H, B, phone, G, bubbles) {
        var o = SvgOut(), p = new Pen(o), figs = layout(W, H, B, phone);
        fixFacing(figs, W);
        scene(p, figs, G, 0, 0, true, H, W);
        var placed = [];
        (bubbles || CALM).forEach(function (b) {
          var F = figs.filter(function (f) { return f.id === b[0]; })[0]; if (!F) return;
          var tw = b[1].length * 8.2 + 24, x = Math.max(8, Math.min(W - tw - 8, F.x - tw / 2)), y = bubY(F) - 40, sh = b[2] === 1, tx = Math.max(x + 14, Math.min(x + tw - 14, F.x));
          placed.forEach(function (o) { if (x < o[0] + o[2] + 4 && x + tw + 4 > o[0] && y < o[1] + 31 && y + 31 > o[1]) y = o[1] - 44; }); placed.push([x, y, tw]);
          if (x < B.wx1 - 24 && x + tw > B.wx0 + 24 && y < B.wm[1] - 30 && y + 36 > B.wm[0] + 20) { placed.pop(); return; }
          p.r = rng(F.seed); p.m = [1, 0, 0, 1, 0, 0];
          p.shape(rect(x, y, tw, 27, 12), { close: 1, fill: sh ? LAMP : WHITE, w: sh ? 2.5 : 2, col: sh ? INK : LINE, amp: .5 });
          p.shape([[tx - 7, y + 26], [tx - 1, y + 36], [tx + 6, y + 26]], { fill: sh ? LAMP : WHITE, w: sh ? 2.5 : 2, col: sh ? INK : LINE, amp: .3 });
          o.raw('<text x="' + Math.round(x + tw / 2) + '" y="' + (y + 18.5) + '" text-anchor="middle" font-family="Nunito,ui-rounded,system-ui,sans-serif" font-weight="800" font-size="15" fill="#16150F">' + b[1].replace(/'/g, "&#8217;") + "</text>");
        });
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="xMidYMid slice" stroke-linecap="round" stroke-linejoin="round"><defs>' + o.defs.join("") + "</defs>" + o.parts.join("") + "</svg>";
      },
      Pen: Pen, SvgOut: SvgOut, DRAW: DRAW, rng: rng, rect: rect, ell: ell, hatch: hatch, giant: giant, book: book,
      legs: legs, body: body, head: head, arm: arm, C: { INK: INK, WHITE: WHITE, CREAM: CREAM, LAMP: LAMP, LAMPD: LAMPD, WATER: WATER, AID: AID, HATCH: HATCH, HATCHB: HATCHB }
    };
    return;
  }

  /* ---------- browser ---------- */
  // The hero's words, buttons and wordmark, measured. tools/art/measure.js prints
  // this (LastbookCrowd.take()) so make-still.cjs can draw the same scene as SVG.
  function take() {
    var hero = document.getElementById("hero"), r = hero.getBoundingClientRect(), wm = document.querySelector(".wm-svg").getBoundingClientRect(),
      c = document.getElementById("centre").getBoundingClientRect(), bar = document.querySelector(".bar").getBoundingClientRect();
    var W = r.width, H = r.height, phone = W < 760, rs = [], tx = [];
    [].forEach.call(document.querySelectorAll("#centre .wm-svg,#centre .lead,#centre .phone-note,#centre .dl-item,#centre .then,#centre .hero-community,#centre .launch-headline,#centre .launch-description,#centre .launch-path,.hero-foot p,.bar-in>*"), function (el) {
      var q = el.getBoundingClientRect(); if (!q.width) return;
      var b = [q.left - r.left - 16, q.top - r.top - 16, q.right - r.left + 16, q.bottom - r.top + 10];
      rs.push(b); if (!el.classList.contains("wm-svg")) tx.push(b);
    });
    var top = bar.bottom - r.top, ws = wm.width / 620, anchor = wm.top - r.top + 26 * ws;
    var G = { x: wm.left - r.left + 318 * ws, s: Math.max(.05, Math.min(ws * (phone ? .5 : .6), (anchor - top - 10) / 306)), top: top };
    G.y = anchor - 306 * G.s;
    return { W: W, H: H, phone: phone, G: G,
      B: { rects: rs, text: tx, cx: (c.left + c.right) / 2 - r.left, cy: (c.top + c.bottom) / 2 - r.top, top: top, wm: [wm.top - r.top, wm.bottom - r.top], wx0: wm.left - r.left, wx1: wm.right - r.left } };
  }
  window.LastbookCrowd = { take: take };
  var RM = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)"), reduce = !!(RM && RM.matches);
  function start() {
    var hero = document.getElementById("hero"), cv = document.getElementById("crowd"), centre = document.getElementById("centre"), bl = document.getElementById("bubbles");
    if (!hero || !cv || !cv.getContext) return;
    var ctx = cv.getContext("2d"), pen = new Pen(CanvasOut(ctx)), figs = [], G = null, W = 0, H = 0, dpr = 1, phone = false, B;
    function measure() {
      var L = take();
      W = L.W; H = L.H; B = L.B; G = L.G; phone = L.phone; dpr = Math.min(2, window.devicePixelRatio || 1);
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      figs = layout(W, H, B, phone); fixFacing(figs, W);
    }
    measure();
    var sceneStart = performance.now();
    var bubs = [], qi = 0, nextBub = 900, last = performance.now(), running = true, raf = 0, seen = true, mx = null, my = null;
    function makeBub(text, kind, F) {
      var d = document.createElement("div"); d.className = "bub" + (kind === 1 ? " shout" : kind === 2 ? " quiet" : "");
      var s = document.createElement("span"); s.textContent = text; d.appendChild(s);
      d.insertAdjacentHTML("beforeend", '<svg width="18" height="14" viewBox="0 0 18 14" aria-hidden="true"><path d="M2 0.5 L8.5 12 L15.5 0.5"/></svg>');
      bl.appendChild(d);
      var b = { el: d, F: F, born: performance.now(), life: 2200 + Math.random() * 1000 };
      place(b); requestAnimationFrame(function () { d.classList.add("on"); });
      return b;
    }
    function place(b) {
      // a hidden bubble measures 0 wide, so keep the size from when it last showed
      if (!b.el.hidden || !b.w) { var wasHidden = b.el.hidden; b.el.hidden = false; b.w = b.el.offsetWidth; b.h = b.el.offsetHeight; b.el.hidden = wasHidden; }
      var F = b.F, w = b.w, h = b.h, x = F.x - w / 2, y = bubY(F) - h - 6, top = B.top + 8;
      // keep it off the words: slide it sideways out of any text it would cover
      for (var j = 0; j < B.text.length; j++) { var t = B.text[j]; if (x < t[2] - 14 && x + w > t[0] + 14 && y < t[3] - 6 && y + h > t[1] + 12) x = F.x < B.cx ? t[0] + 14 - w : t[2] - 14; }
      x = Math.max(8, Math.min(W - w - 8, x));
      // keep clear of the other bubbles: step up above any it would cover
      for (var i = 0; i < bubs.length; i++) { var o = bubs[i].r; if (bubs[i] !== b && o && x < o[0] + o[2] + 4 && x + w + 4 > o[0] && y < o[1] + o[3] + 4 && y + h + 4 > o[1]) y = o[1] - h - 4; }
      // no room on screen, or it would sit under the header: don't show it
      // nor on top of the wordmark
      var onWm = x < B.wx1 - 24 && x + w > B.wx0 + 24 && y < B.wm[1] - 30 && y + h > B.wm[0] + 20;
      b.el.hidden = w > W - 16 || y < top || onWm || x < 0 || x + w > W || !!hits([x, y, x + w, y + h], B.text);
      b.r = b.el.hidden ? null : [x, y, w, h];
      b.el.style.transform = "translate(" + Math.round(x) + "px," + Math.round(y) + "px)";
    }
    function byId(id) { for (var i = 0; i < figs.length; i++) if (figs[i].id === id) return figs[i]; return null; }
    function calm() { CALM.forEach(function (s) { var F = byId(s[0]); if (F) bubs.push(makeBub(s[1], s[2], F)); }); }
    if (reduce) calm();

    function draw(now) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
      scene(pen, figs, G, now - sceneStart, reduce ? 0 : Math.floor(now / 160) % 3, reduce, H, W);
    }
    function tick(now) {
      raf = 0; if (!running) return;
      var dt = Math.min(.05, (now - last) / 1000); last = now;
      figs.forEach(function (F) {
        if (!F.walk) return;
        var nx = F.x + F.dir * F.speed * dt, bx = boxOf(nx, F.y, F.s);
        if (hits(bx, F.far ? B.text : B.rects) || hitsFig(bx, figs, F)) { F.dir = -F.dir; return; }
        F.x = nx;
        if (F.x > F.hi) { F.x = F.hi; F.dir = -1; } if (F.x < F.lo) { F.x = F.lo; F.dir = 1; }
      });
      nextBub -= dt * 1000;
      if (nextBub <= 0 && bubs.length < 3) {
        for (var n = 0; n < STORY.length; n++) {
          var s = STORY[qi++ % STORY.length], F = byId(s[0]);
          if (F && !bubs.some(function (b) { return b.F === F; })) { bubs.push(makeBub(s[1], s[2], F)); break; }
        }
        nextBub = 900 + Math.random() * 500;
      }
      bubs = bubs.filter(function (b) {
        var age = now - b.born;
        if (age > b.life + 200) { b.el.remove(); return false; }
        if (age > b.life) b.el.classList.remove("on");
        place(b); return true;
      });
      draw(now); eyes(now);
      raf = requestAnimationFrame(tick);
    }
    var paused = false, pb = document.getElementById("pause");
    function wirePause() {
      pb.dataset.on = "1";
      pb.addEventListener("click", function () { paused = !paused; pb.setAttribute("aria-pressed", paused ? "true" : "false"); pb.querySelector("span").textContent = paused ? "Play" : "Pause"; go(); });
    }
    if (pb && !reduce) { pb.hidden = false; wirePause(); }
    if (RM) {
      var onRM = function () {
        reduce = RM.matches;
        bubs.forEach(function (b) { b.el.remove(); }); bubs = [];
        if (pb) pb.hidden = reduce;
        if (reduce) { if (raf) cancelAnimationFrame(raf); raf = 0; running = false; calm(); draw(performance.now()); }
        else { if (pb && !pb.dataset.on) wirePause(); running = false; go(); }
      };
      if (RM.addEventListener) RM.addEventListener("change", onRM); else if (RM.addListener) RM.addListener(onRM);
    }
    function go() { if (reduce) return; var on = seen && !document.hidden && !paused; if (on && !running) { running = true; last = performance.now(); } running = on; if (on && !raf) raf = requestAnimationFrame(tick); }
    draw(performance.now());
    if (!reduce) { raf = requestAnimationFrame(tick); }
    document.addEventListener("visibilitychange", go);
    if (window.IntersectionObserver) new IntersectionObserver(function (es) { seen = es[0].isIntersecting; go(); }).observe(hero);
    var rt, lastW = window.innerWidth; window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { measure(); bubs.forEach(function (b) { b.el.remove(); }); bubs = []; if (reduce) calm(); draw(performance.now()); }, 150); });
    // the web font can arrive after the first layout and widen the words: measure again then
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { measure(); bubs.forEach(function (b) { b.el.remove(); }); bubs = []; if (reduce) calm(); draw(performance.now()); });
    window.addEventListener("pointermove", function (e) { mx = e.clientX; my = e.clientY; }, { passive: true });

    /* the eyes in BOOK: read along the lines, blink, sometimes glance up */
    var EL = [document.getElementById("pupil-l"), document.getElementById("pupil-r")], LIDS = [document.getElementById("lid-l"), document.getElementById("lid-r")];
    var C = [[240, 302], [396, 304]], BASE = [[5, 16], [-5, 16]], nextBlink = 3000 + Math.random() * 3000, blinkAt = -1, nextGlance = 9000, glanceAt = -1, t0 = performance.now();
    function eyes(now) {
      if (!EL[0]) return;
      var t = now - t0, rd = (t % 2400) / 2400, line = Math.floor(t / 2400) % 3;
      var dx = rd < .93 ? -5 + 10 * (rd / .93) : 5 - 10 * ((rd - .93) / .07), dy = line * 2.2 - 2, lid = 31;
      if (t > nextGlance) { glanceAt = t; nextGlance = t + 13000 + Math.random() * 5000; }
      if (glanceAt >= 0 && t - glanceAt < 900) {
        var gx = 0, gy = -30;
        if (mx != null) { var r = document.querySelector(".wm-svg").getBoundingClientRect(), ex = r.left + r.width * .51, ey = r.top + r.height * .64; gx = (mx - ex) / 40; gy = (my - ey) / 40; }
        var m = Math.hypot(gx, gy) || 1, k = Math.min(1, 21 / m); dx = gx * k; dy = gy * k - 16; lid = 14;
      }
      if (t > nextBlink) { blinkAt = t; nextBlink = t + 4000 + Math.random() * 3000; }
      if (blinkAt >= 0 && t - blinkAt < 170) { var b = (t - blinkAt) / 170; lid = 31 + (68 - 31) * (b < .5 ? b * 2 : 2 - b * 2); }
      for (var i = 0; i < 2; i++) {
        var px = C[i][0] + BASE[i][0] + dx, py = C[i][1] + BASE[i][1] + dy;
        EL[i].setAttribute("transform", "translate(" + px.toFixed(1) + " " + py.toFixed(1) + ")");
        var top = C[i][1] - 52, x0 = C[i][0] - 50, x1 = C[i][0] + 50;
        LIDS[i].setAttribute("d", "M" + x0 + " " + top + "H" + x1 + "V" + (top + lid) + "Q" + C[i][0] + " " + (top + lid + 12) + " " + x0 + " " + (top + lid) + "Z");
      }
    }
  }
  // [who, words, 1 = yellow "answer" bubble, 2 = faint grey bubble]
  var STORY = [["lantern", "got the generator working!", 1], ["phone", "built a rain collector!", 1], ["jug", "found clean water!", 1], ["aid", "preserved our first harvest!", 1], ["radio", "got the solar panels working!", 1],
    ["big1", "patched the roof!", 1], ["reader", "bandaged the cut!", 1], ["kid", "built a water filter!", 1], ["garden", "our first tomatoes!", 1], ["x1", "found a water reservoir!", 1]];
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();

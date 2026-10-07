"""Browser stability probes. Pixel checks remain necessary for canvas/CSS UIs."""

STABILITY_JS = r"""() => {
  if (!window.__guaStability) {
    const state = {changed: performance.now(), seq: 0, document: String(performance.timeOrigin)};
    const touch = () => { state.changed = performance.now(); state.seq++; };
    const seen = new WeakSet();
    const canvases = new Set(), hashes = new WeakMap();
    const probe = document.createElement('canvas');
    probe.width = probe.height = 16;
    const ctx = probe.getContext('2d', {willReadFrequently:true});
    let scheduled = false, sampled = 0;
    const tick = now => {
      scheduled = false;
      if (now - sampled >= 40) {
        sampled = now;
        for (const canvas of canvases) {
          if (!canvas.isConnected) { canvases.delete(canvas); continue; }
          const box = canvas.getBoundingClientRect();
          if (!ctx || !canvas.width || !canvas.height || box.right <= 0 || box.bottom <= 0 ||
              box.left >= innerWidth || box.top >= innerHeight || !box.width || !box.height) continue;
          try {
            ctx.clearRect(0,0,16,16); ctx.drawImage(canvas,0,0,16,16);
            const data = ctx.getImageData(0,0,16,16).data;
            let hash = 2166136261;
            for (const v of data) hash = Math.imul(hash ^ v, 16777619);
            if (hashes.has(canvas) && hashes.get(canvas) !== hash) touch();
            hashes.set(canvas,hash);
          } catch (_) {
            // Tainted/WebGL surfaces still use the independent screenshot checks.
            probe.width = 16;
          }
        }
      }
      if (canvases.size) { scheduled = true; requestAnimationFrame(tick); }
    };
    const addCanvas = canvas => {
      canvases.add(canvas);
      if (!scheduled) { scheduled = true; requestAnimationFrame(tick); }
    };
    const attach = root => {
      if (seen.has(root)) return;
      seen.add(root);
      const observer = new MutationObserver(records => {
        if (records.some(r => r.type !== 'attributes' || !r.attributeName.startsWith('data-gua-'))) touch();
        for (const r of records) for (const node of r.addedNodes) scan(node);
      });
      observer.observe(root, {subtree:true, childList:true, characterData:true, attributes:true});
      scan(root);
    };
    const scan = node => {
      if (node.tagName === 'CANVAS') addCanvas(node);
      if (node.shadowRoot) attach(node.shadowRoot);
      if (node.querySelectorAll) for (const el of node.querySelectorAll('*')) {
        if (el.shadowRoot) attach(el.shadowRoot);
        if (el.tagName === 'CANVAS') addCanvas(el);
      }
    };
    attach(document);
    for (const event of ['input', 'change', 'scroll', 'resize']) window.addEventListener(event, touch, true);
    window.__guaStability = state;
  }
  const s = window.__guaStability;
  // An infinite decorative spinner must not keep the task locked forever.
  // The verifier separately checks busy/progress signals before claiming done.
  const animating = document.getAnimations().some(a => a.playState === 'running' &&
    a.effect && Number.isFinite(a.effect.getComputedTiming().endTime));
  return {document:s.document, seq:s.seq, quiet:performance.now()-s.changed,
    ready:document.readyState !== 'loading', animating};
}"""

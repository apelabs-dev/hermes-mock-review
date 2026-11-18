/* Render the flattened export document to a PNG with vendored SnapDOM and download it. Comments are off in the
   page the server builds for this: the capture never sees comment UI. */
(function () {
  'use strict';

  var status = null;

  function setStatus(text, kind) {
    if (!status) {
      status = document.createElement('p');
      status.className = 'mr-capture-status';
      status.setAttribute('role', 'status');
      status.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:10;margin:0;padding:6px 10px;' +
        'border-radius:8px;background:#18181b;color:#fff;font:12px/1.4 sans-serif;max-width:calc(100% - 24px)';
      document.body.appendChild(status);
    }
    status.textContent = text;
    status.style.background = kind === 'error' ? '#b91c1c' : '#18181b';
  }

  function filename() {
    var name = document.body.getAttribute('data-export-filename') || 'export.png';
    return /\.png$/i.test(name) ? name : name + '.png';
  }

  function download(href, name) {
    var a = document.createElement('a');
    a.href = href;
    a.download = name;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  function run() {
    var width = (function () {
      try {
        var q = new URLSearchParams(location.search).get('width');
        var n = parseInt(q, 10);
        return isFinite(n) ? Math.max(320, Math.min(1920, n)) : 1280;
      } catch (e) { return 1280; }
    })();
    if (document.body.getBoundingClientRect().width !== width) {
      document.body.style.width = width + 'px';
      document.body.style.maxWidth = width + 'px';
      document.body.style.margin = '0 auto';
    }
    if (!window.snapdom || typeof window.snapdom.toPng !== 'function') {
      setStatus('Could not render the image: SnapDOM did not load.', 'error');
      return;
    }
    setStatus('Rendering image…');
    var fonts = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
    fonts.then(function () {
      return new Promise(function (resolve) { requestAnimationFrame(function () { resolve(); }); });
    }).then(function () {
      return window.snapdom.toPng(document.body, {
        scale: window.devicePixelRatio || 1,
        backgroundColor: getComputedStyle(document.body).backgroundColor
      });
    }).then(function (image) {
      var name = filename();
      var href = image && (image.src || image.getAttribute && image.getAttribute('src'));
      if (!href) throw new Error('SnapDOM returned no image data');
      download(href, name);
      setStatus('Saved ' + name);
    }).catch(function (e) {
      setStatus('Could not render the image: ' + String((e && e.message) || e), 'error');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();

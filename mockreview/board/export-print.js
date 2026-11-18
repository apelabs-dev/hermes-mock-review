/* The print page for an export: the same document as the HTML export, printed once when it has loaded. */
(function () {
  'use strict';
  window.addEventListener('load', function () {
    window.print();
  });
})();

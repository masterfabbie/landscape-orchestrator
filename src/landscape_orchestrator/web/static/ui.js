// Small helpers for the interface. Everything works without them; they only make it nicer.
(function () {
  "use strict";

  // Landscape tree: remember which systems are open, so the automatic reload while an
  // activity runs does not collapse them. Kept per browser tab (sessionStorage).
  var KEY = "lo.tree.open";
  var open = [];
  try {
    open = JSON.parse(sessionStorage.getItem(KEY) || "[]");
  } catch (e) {
    open = [];
  }
  function save() {
    try {
      sessionStorage.setItem(KEY, JSON.stringify(open));
    } catch (e) {
      // storage not available: the tree still works, it just forgets
    }
  }
  function remember(key, isOpen) {
    var index = open.indexOf(key);
    if (isOpen && index < 0) open.push(key);
    if (!isOpen && index >= 0) open.splice(index, 1);
  }
  var toggles = document.querySelectorAll("input.tree-toggle[data-key]");
  toggles.forEach(function (box) {
    if (open.indexOf(box.dataset.key) >= 0) box.checked = true;
    box.addEventListener("change", function () {
      remember(box.dataset.key, box.checked);
      save();
    });
  });
  document.querySelectorAll("[data-tree-all]").forEach(function (button) {
    button.hidden = false;
    button.addEventListener("click", function () {
      var expand = button.dataset.treeAll === "open";
      toggles.forEach(function (box) {
        box.checked = expand;
        remember(box.dataset.key, expand);
      });
      save();
    });
  });

  // Menus: only one open at a time; a click outside closes them.
  var menus = document.querySelectorAll("details.menu");
  menus.forEach(function (menu) {
    menu.addEventListener("toggle", function () {
      if (!menu.open) return;
      menus.forEach(function (other) {
        if (other !== menu) other.open = false;
      });
    });
  });
  document.addEventListener("click", function (event) {
    menus.forEach(function (menu) {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
  });
})();

(function () {
  "use strict";

  var STORAGE_KEY = "status-portal-landing-theme";
  var root = document.documentElement;
  var toggle = document.getElementById("theme-toggle");

  function currentlyDark() {
    var explicit = root.getAttribute("data-theme");
    if (explicit === "dark") return true;
    if (explicit === "light") return false;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function syncToggleLabel() {
    if (!toggle) return;
    var dark = currentlyDark();
    toggle.textContent = dark ? "☀️" : "🌙";
    toggle.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
  }

  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = currentlyDark() ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(STORAGE_KEY, next); } catch (e) {}
      syncToggleLabel();
    });
  }
  syncToggleLabel();

  // Mobile nav
  var navToggle = document.getElementById("nav-toggle");
  var mobileMenu = document.getElementById("mobile-menu");
  if (navToggle && mobileMenu) {
    navToggle.addEventListener("click", function () {
      var open = mobileMenu.classList.toggle("open");
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    mobileMenu.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function () {
        mobileMenu.classList.remove("open");
        navToggle.setAttribute("aria-expanded", "false");
      });
    });
  }

  // Gallery filter tabs
  var tabs = document.querySelectorAll(".gallery-tab");
  var items = document.querySelectorAll(".gallery-item");
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      tabs.forEach(function (t) { t.classList.remove("active"); });
      tab.classList.add("active");
      var filter = tab.getAttribute("data-filter");
      items.forEach(function (item) {
        var matches = filter === "all" || item.getAttribute("data-group") === filter;
        item.classList.toggle("hidden", !matches);
      });
    });
  });

  // Reveal-on-scroll
  var revealEls = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && revealEls.length) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    revealEls.forEach(function (el) { observer.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add("in"); });
  }

  // Footer year
  var yearEl = document.getElementById("year");
  if (yearEl) yearEl.textContent = new Date().getFullYear();
})();

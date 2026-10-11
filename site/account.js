(function () {
  var STORAGE = "reviewgrade.account";
  var blank = {
    email: "ada@example.com",
    cap: 50,
    finished: 36,
    included: 250,
    keys: [
      { name: "ci", prefix: "ra_live_7kQm3aP1", last: "2 Oct", revoked: false },
      { name: "agent", prefix: "ra_live_p2nR8cQw", last: "", revoked: false }
    ]
  };

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function load() {
    try {
      var raw = sessionStorage.getItem(STORAGE);
      if (!raw) return clone(blank);
      var data = JSON.parse(raw);
      if (!data || typeof data.cap !== "number" || !Array.isArray(data.keys)) return clone(blank);
      if (typeof data.email !== "string") return clone(blank);
      if (typeof data.finished !== "number" || typeof data.included !== "number") return clone(blank);
      return data;
    } catch (err) {
      return clone(blank);
    }
  }

  function save() {
    try {
      sessionStorage.setItem(STORAGE, JSON.stringify({
        email: state.email,
        cap: state.cap,
        finished: state.finished,
        included: state.included,
        keys: state.keys
      }));
    } catch (err) {
      return;
    }
  }

  function token(length) {
    var alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    var bytes = new Uint8Array(length);
    if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
    else {
      for (var j = 0; j < length; j++) bytes[j] = Math.floor(Math.random() * 256);
    }
    var out = "";
    for (var i = 0; i < length; i++) out += alphabet[bytes[i] % alphabet.length];
    return out;
  }

  var state = load();
  var reveal = "";

  function left() {
    return Math.max(0, state.included - state.finished);
  }

  function capLine() {
    return "After 250 reviews, each one is $0.15 until the extra charge reaches $" + String(state.cap) + ".";
  }

  function note(id, text) {
    var el = document.querySelector(id);
    el.textContent = text;
    el.hidden = text === "";
  }

  function render() {
    document.querySelector("#email").textContent = state.email;
    document.querySelector("#left").textContent = String(left());
    document.querySelector("#used-line").textContent = state.finished + " of " + state.included + " are finished. Unused reviews end with the month.";
    var capInput = document.querySelector("#cap");
    if (document.activeElement !== capInput) capInput.value = String(state.cap);
    document.querySelector("#cap-line").textContent = capLine();

    var list = document.querySelector("#key-list");
    list.replaceChildren();
    state.keys.forEach(function (key, index) {
      var row = document.createElement("div");
      row.className = "key";
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = key.name;
      var prefix = document.createElement("span");
      prefix.className = "prefix";
      prefix.textContent = key.prefix;
      var used = key.last ? "Last used " + key.last + "." : "Not used yet.";
      var detail = document.createElement("p");
      detail.className = "detail";
      detail.textContent = used;
      row.append(name, prefix);
      if (key.revoked) {
        var word = document.createElement("span");
        word.className = "name";
        word.textContent = "Revoked";
        row.append(word);
      } else {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "act";
        button.textContent = "Revoke";
        button.addEventListener("click", function () {
          state.keys[index].revoked = true;
          if (reveal.indexOf(key.prefix) === 0) reveal = "";
          save();
          render();
        });
        row.append(button);
      }
      row.append(detail);
      list.append(row);
    });

    var secret = document.querySelector("#secret");
    var code = document.querySelector("#secret-value");
    if (reveal) {
      secret.hidden = false;
      code.textContent = reveal;
    } else {
      secret.hidden = true;
      code.textContent = "";
    }
  }

  var capInput = document.querySelector("#cap");

  function readCap(restore) {
    var raw = capInput.value.trim();
    if (!/^\d+$/.test(raw)) {
      if (restore) {
        capInput.value = String(state.cap);
        note("#cap-note", "");
        document.querySelector("#cap-line").textContent = capLine();
      } else if (raw !== "") {
        note("#cap-note", "Use a whole number of dollars, from 0 up.");
      } else {
        note("#cap-note", "");
      }
      return;
    }
    note("#cap-note", "");
    state.cap = Number(raw);
    save();
    document.querySelector("#cap-line").textContent = capLine();
  }

  capInput.addEventListener("input", function () { readCap(false); });
  capInput.addEventListener("blur", function () { readCap(true); });
  document.querySelector("#cap-form").addEventListener("submit", function (event) {
    event.preventDefault();
    capInput.blur();
  });

  document.querySelector("#key-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var name = document.querySelector("#key-name").value.trim();
    if (!name) {
      note("#key-note", "Name the key.");
      return;
    }
    note("#key-note", "");
    var secret = "ra_live_" + token(24);
    state.keys.push({
      name: name,
      prefix: secret.slice(0, 16),
      last: "",
      revoked: false
    });
    reveal = secret;
    document.querySelector("#key-name").value = "";
    save();
    render();
  });

  document.querySelector("#secret-hide").addEventListener("click", function () {
    reveal = "";
    render();
  });

  render();
})();

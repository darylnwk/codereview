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

  function note(id, text) {
    var el = document.querySelector(id);
    el.textContent = text;
    el.hidden = text === "";
  }

  function render() {
    document.querySelector("#email").textContent = state.email;
    document.querySelector("#left").textContent = String(left());
    document.querySelector("#running-credits").textContent = String(left()) + " credits";
    document.querySelector("#used-line").textContent = state.finished + " of the " + state.included + " reviews are finished. Unused reviews end with the month.";
    document.querySelector("#cap").value = String(state.cap);
    document.querySelector("#cap-line").textContent = "Reviews after the 250 stop when the extra charge reaches $" + String(state.cap) + ".";

    var list = document.querySelector("#key-list");
    list.replaceChildren();
    state.keys.forEach(function (key, index) {
      var row = document.createElement("div");
      row.className = "offer";
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = key.name;
      var leaders = document.createElement("span");
      leaders.className = "leaders";
      leaders.setAttribute("aria-hidden", "true");
      var price = document.createElement("span");
      price.className = "price";
      price.textContent = key.prefix;
      row.append(name, leaders, price);

      var detail = document.createElement("p");
      detail.className = "detail";
      var used = key.last ? "Last used " + key.last + "." : "Not used yet.";
      if (key.revoked) {
        detail.textContent = "Revoked. " + used;
      } else {
        detail.append(document.createTextNode(used + " "));
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
        detail.append(button);
      }
      list.append(row, detail);
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

  document.querySelector("#cap-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var raw = document.querySelector("#cap").value.trim();
    if (!/^\d+$/.test(raw)) {
      note("#cap-note", "The cap is a whole number of dollars, from 0 up.");
      document.querySelector("#cap").value = String(state.cap);
      return;
    }
    note("#cap-note", "");
    state.cap = Number(raw);
    save();
    render();
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

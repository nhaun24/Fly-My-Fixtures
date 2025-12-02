(() => {
  'use strict';

  const TAB_STORAGE_KEY = 'td.activeTab';
  const FIXTURE_LIMIT = 6;

  let NETWORK_ADAPTERS = null;
  let USB_DEVICES = [];
  let CAPTURE_STATE = null;
  let CAPTURE_POLL_TIMER = null;
  let restartConfirmTimer = null;

  let BTN_ACTIVATE = 0;
  let BTN_RELEASE = 0;
  let BTN_FLASH10 = 0;
  let BTN_DIMOFF = 0;
  let BTN_FINE = 0;
  let BTN_ZOOM = 0;

  let POSITION_PRESETS = [];
  let PRESET_BUTTONS = [];
  let PRESET_STATUS_TIMER = null;

  /* ------------------------------------------------------------------------ */
  /* Utility helpers                                                         */
  /* ------------------------------------------------------------------------ */

  async function fetchJSON(url, opts) {
    const response = await fetch(url, opts);
    const contentType = response.headers.get('content-type') || '';
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return contentType.includes('application/json') ? response.json() : response.text();
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) {
      return '0 B';
    }
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let result = value;
    let unitIndex = 0;
    while (result >= 1024 && unitIndex < units.length - 1) {
      result /= 1024;
      unitIndex += 1;
    }
    const decimals = result >= 10 || unitIndex === 0 ? 0 : 1;
    return `${result.toFixed(decimals)} ${units[unitIndex]}`;
  }

  function formatDuration(seconds) {
    const total = Math.max(0, Number(seconds) || 0);
    if (total >= 3600) {
      const hours = Math.floor(total / 3600);
      const mins = Math.floor((total % 3600) / 60);
      return `${hours}h ${mins}m`;
    }
    if (total >= 60) {
      const mins = Math.floor(total / 60);
      const secs = Math.floor(total % 60);
      return `${mins}m ${secs}s`;
    }
    return `${Math.floor(total)}s`;
  }

  function parseErrorMessage(err) {
    if (!err) return 'Unexpected error';
    if (typeof err === 'string') return err;
    if (err.error) return err.error;
    if (err.message) {
      try {
        const parsed = JSON.parse(err.message);
        if (parsed && parsed.error) return parsed.error;
      } catch (_err) {
        /* ignore JSON parse error */
      }
      return err.message;
    }
    return String(err);
  }

  function isCheckbox(el) {
    return !!el && el.type === 'checkbox';
  }

  function formatPresetValue(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return '—';
    return Math.round(num).toString();
  }

  function setPresetStatus(message, type = 'info') {
    const el = document.getElementById('preset-status');
    if (!el) return;
    const normalized = type === 'success' || type === 'error' ? type : 'info';
    el.textContent = message || '';
    el.className = `status-line ${normalized}`;
    if (PRESET_STATUS_TIMER) {
      clearTimeout(PRESET_STATUS_TIMER);
      PRESET_STATUS_TIMER = null;
    }
    if (message) {
      PRESET_STATUS_TIMER = setTimeout(() => {
        const target = document.getElementById('preset-status');
        if (!target) return;
        target.textContent = '';
        target.className = 'status-line info';
      }, 4000);
    }
  }

  /* ------------------------------------------------------------------------ */
  /* Network adapters & packet capture                                        */
  /* ------------------------------------------------------------------------ */

  async function ensureNetworkAdapters() {
    if (Array.isArray(NETWORK_ADAPTERS)) {
      return NETWORK_ADAPTERS;
    }
    try {
      const resp = await fetchJSON('/api/network/adapters');
      NETWORK_ADAPTERS = Array.isArray(resp.adapters) ? resp.adapters : [];
    } catch (error) {
      NETWORK_ADAPTERS = [];
      console.error('Failed to load network adapters', error);
    }
    return NETWORK_ADAPTERS;
  }

  function syncSacnInterfaces() {
    const container = document.getElementById('sacn-iface-list');
    const hidden = document.getElementById('sacn_bind_addresses');
    if (!hidden) return;

    const selected = [];
    if (container) {
      container
        .querySelectorAll('input[type="checkbox"][data-addr]')
        .forEach((checkbox) => {
          if (checkbox.checked) {
            selected.push(checkbox.dataset.addr);
          }
        });
    }
    hidden.value = JSON.stringify(selected);
  }

  function renderCaptureInterfaceOptions(selectedIface) {
    const select = document.getElementById('pcap-interface');
    const noMsg = document.getElementById('pcap-no-ifaces');
    if (!select) {
      if (noMsg) noMsg.style.display = 'none';
      return;
    }

    const adapters = Array.isArray(NETWORK_ADAPTERS) ? NETWORK_ADAPTERS : [];
    const seen = new Set();
    const previous = select.value;
    const target = selectedIface || previous || '';
    select.innerHTML = '';

    if (!adapters.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'No adapters available';
      select.appendChild(opt);
      select.value = '';
      select.disabled = true;
      if (noMsg) noMsg.style.display = 'block';
      return;
    }

    if (noMsg) noMsg.style.display = 'none';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Select interface…';
    select.appendChild(placeholder);

    adapters.forEach((adapter) => {
      const name = adapter && adapter.name ? String(adapter.name) : '';
      if (!name || seen.has(name)) return;
      seen.add(name);
      const option = document.createElement('option');
      option.value = name;
      option.textContent = adapter.address ? `${name} – ${adapter.address}` : name;
      select.appendChild(option);
    });

    if (target && seen.has(target)) {
      select.value = target;
    } else {
      select.value = '';
    }
    select.disabled = false;
  }

  function renderNetworkAdapters(selected) {
    const container = document.getElementById('sacn-iface-list');
    const hidden = document.getElementById('sacn_bind_addresses');
    if (!container) {
      if (hidden) hidden.value = JSON.stringify(selected || []);
      return;
    }

    const adapters = Array.isArray(NETWORK_ADAPTERS) ? NETWORK_ADAPTERS : [];
    const selectedSet = new Set((selected || []).map((value) => String(value)));
    container.innerHTML = '';

    if (!adapters.length) {
      const msg = document.createElement('p');
      msg.className = 'small muted';
      msg.textContent = 'No network adapters detected.';
      container.appendChild(msg);
    } else {
      adapters.forEach((adapter, index) => {
        const id = `iface-${index}`;
        const wrapper = document.createElement('label');
        wrapper.className = 'checklist-option';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.id = id;
        checkbox.dataset.addr = adapter.address;
        checkbox.checked = selectedSet.has(String(adapter.address));
        wrapper.appendChild(checkbox);

        const textWrap = document.createElement('div');
        textWrap.className = 'checklist-text';

        const title = document.createElement('div');
        title.className = 'checklist-title';
        title.textContent = adapter.label || `${adapter.name} – ${adapter.address}`;
        textWrap.appendChild(title);

        if (adapter.description) {
          const desc = document.createElement('small');
          desc.className = 'muted';
          desc.textContent = adapter.description;
          textWrap.appendChild(desc);
        } else if (adapter.is_loopback) {
          const note = document.createElement('small');
          note.className = 'muted';
          note.textContent = 'Loopback';
          textWrap.appendChild(note);
        }

        wrapper.appendChild(textWrap);
        container.appendChild(wrapper);
      });
    }

    if (!container.dataset.bound) {
      container.addEventListener('change', syncSacnInterfaces);
      container.dataset.bound = 'true';
    }

    syncSacnInterfaces();
    const iface = CAPTURE_STATE && CAPTURE_STATE.interface ? CAPTURE_STATE.interface : '';
    renderCaptureInterfaceOptions(iface);
  }

  async function refreshNetworkAdapters(selected) {
    await ensureNetworkAdapters();
    renderNetworkAdapters(selected);
  }

  /* ------------------------------------------------------------------------ */
  /* USB devices                                                              */
  /* ------------------------------------------------------------------------ */

  async function refreshUsbDevices(preferred) {
    const select = document.getElementById('usb_device');
    const emptyMsg = document.getElementById('usb-device-empty');
    const refreshBtn = document.getElementById('usb-refresh-btn');

    if (!select) return;

    if (refreshBtn) refreshBtn.disabled = true;
    select.disabled = true;
    select.innerHTML = '<option value="">Loading…</option>';

    let selected = typeof preferred === 'string' ? preferred : '';

    try {
      const resp = await fetchJSON('/api/usb/devices');
      USB_DEVICES = Array.isArray(resp.devices) ? resp.devices : [];
      if (!selected && resp.selected) {
        selected = String(resp.selected);
      }
    } catch (error) {
      console.error('Failed to load USB devices', error);
      USB_DEVICES = [];
    }

    select.innerHTML = '';

    const autoOption = document.createElement('option');
    autoOption.value = '';
    autoOption.textContent = 'Auto-select first device';
    select.appendChild(autoOption);

    const seen = new Set();
    USB_DEVICES.forEach((device, index) => {
      const id = device && device.id ? String(device.id) : `device-${index}`;
      if (seen.has(id)) return;
      seen.add(id);
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = device.label || device.name || id;
      select.appendChild(opt);
    });

    if (emptyMsg) {
      emptyMsg.style.display = USB_DEVICES.length ? 'none' : 'block';
    }

    if (selected && seen.has(selected)) {
      select.value = selected;
    } else {
      select.value = '';
    }

    select.disabled = false;
    if (refreshBtn) refreshBtn.disabled = false;
  }

  /* ------------------------------------------------------------------------ */
  /* Presets                                                                  */
  /* ------------------------------------------------------------------------ */

  function getPresetById(id) {
    return POSITION_PRESETS.find((preset) => String(preset.id) === String(id));
  }

  function getButtonForPreset(id) {
    return PRESET_BUTTONS.find((entry) => String(entry.preset_id) === String(id));
  }

  function updatePresetCurrent(current) {
    const panEl = document.getElementById('preset-cur-pan');
    const tiltEl = document.getElementById('preset-cur-tilt');
    const dimEl = document.getElementById('preset-cur-dimmer');
    const zoomEl = document.getElementById('preset-cur-zoom');
    const source = current || {};
    if (panEl) panEl.textContent = formatPresetValue(source.pan);
    if (tiltEl) tiltEl.textContent = formatPresetValue(source.tilt);
    if (dimEl) dimEl.textContent = formatPresetValue(source.dimmer);
    if (zoomEl) zoomEl.textContent = formatPresetValue(source.zoom);
  }

  function renderPresetList() {
    const container = document.getElementById('preset-list');
    if (!container) return;
    container.innerHTML = '';

    if (!Array.isArray(POSITION_PRESETS) || !POSITION_PRESETS.length) {
      const empty = document.createElement('div');
      empty.className = 'preset-empty';
      empty.textContent = 'No presets saved yet. Capture the current position to create one.';
      container.appendChild(empty);
      return;
    }

    const table = document.createElement('table');
    table.className = 'preset-table';

    const thead = document.createElement('thead');
    thead.innerHTML = `
      <tr>
        <th>Name</th>
        <th>Pan (16-bit)</th>
        <th>Tilt (16-bit)</th>
        <th>Dimmer (8-bit)</th>
        <th>Zoom</th>
        <th>Button</th>
        <th>Actions</th>
      </tr>
    `;
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    POSITION_PRESETS.forEach((preset) => {
      const row = document.createElement('tr');
      row.dataset.id = preset.id;

      const nameCell = document.createElement('td');
      const nameWrap = document.createElement('div');
      nameWrap.className = 'preset-name';
      const nameStrong = document.createElement('strong');
      nameStrong.textContent = preset.name || preset.id;
      nameWrap.appendChild(nameStrong);
      const renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.className = 'btn tiny';
      renameBtn.textContent = 'Rename';
      renameBtn.addEventListener('click', () => renamePreset(preset.id));
      nameWrap.appendChild(renameBtn);
      nameCell.appendChild(nameWrap);
      row.appendChild(nameCell);

      const panCell = document.createElement('td');
      panCell.textContent = formatPresetValue(preset.pan);
      row.appendChild(panCell);

      const tiltCell = document.createElement('td');
      tiltCell.textContent = formatPresetValue(preset.tilt);
      row.appendChild(tiltCell);

      const dimCell = document.createElement('td');
      dimCell.textContent = formatPresetValue(preset.dimmer);
      row.appendChild(dimCell);

      const zoomCell = document.createElement('td');
      zoomCell.textContent = formatPresetValue(preset.zoom);
      row.appendChild(zoomCell);

      const buttonCell = document.createElement('td');
      const buttonEntry = getButtonForPreset(preset.id);
      const input = document.createElement('input');
      input.type = 'number';
      input.className = 'preset-button-input';
      input.min = '0';
      input.step = '1';
      input.placeholder = 'None';
      if (buttonEntry && Number.isFinite(Number(buttonEntry.button))) {
        input.value = buttonEntry.button;
      }
      input.addEventListener('change', () => assignPresetButton(preset.id, input.value));
      buttonCell.appendChild(input);
      row.appendChild(buttonCell);

      const actionsCell = document.createElement('td');
      const actionsWrap = document.createElement('div');
      actionsWrap.className = 'preset-actions';

      const recallBtn = document.createElement('button');
      recallBtn.type = 'button';
      recallBtn.className = 'btn tiny';
      recallBtn.textContent = 'Recall';
      recallBtn.addEventListener('click', () => recallPreset(preset.id));
      actionsWrap.appendChild(recallBtn);

      const updateBtn = document.createElement('button');
      updateBtn.type = 'button';
      updateBtn.className = 'btn tiny';
      updateBtn.textContent = 'Update from Current';
      updateBtn.addEventListener('click', () => updatePresetFromCurrent(preset.id));
      actionsWrap.appendChild(updateBtn);

      const deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.className = 'btn danger tiny';
      deleteBtn.textContent = 'Delete';
      deleteBtn.addEventListener('click', () => deletePreset(preset.id));
      actionsWrap.appendChild(deleteBtn);

      actionsCell.appendChild(actionsWrap);
      row.appendChild(actionsCell);

      tbody.appendChild(row);
    });

    table.appendChild(tbody);
    container.appendChild(table);
  }

  async function loadPresets(showStatus = false) {
    try {
      const data = await fetchJSON('/api/presets');
      POSITION_PRESETS = Array.isArray(data.presets) ? data.presets : [];
      PRESET_BUTTONS = Array.isArray(data.buttons) ? data.buttons : [];
      updatePresetCurrent(data.current || {});
      renderPresetList();
      if (showStatus) {
        setPresetStatus('Preset data refreshed', 'info');
      }
    } catch (error) {
      console.error('Failed to load presets', error);
      setPresetStatus(parseErrorMessage(error), 'error');
    }
  }

  async function refreshPresetState() {
    await loadPresets(true);
  }

  async function savePresetFromCurrent() {
    const nameInput = document.getElementById('preset-name-input');
    const name = nameInput ? nameInput.value.trim() : '';
    try {
      await fetchJSON('/api/presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
      });
      if (nameInput) nameInput.value = '';
      setPresetStatus('Preset saved', 'success');
      await loadPresets();
    } catch (error) {
      console.error('Failed to save preset', error);
      setPresetStatus(parseErrorMessage(error), 'error');
    }
  }

  async function recallPreset(presetId) {
    try {
      await fetchJSON(`/api/presets/${encodeURIComponent(presetId)}/recall`, {
        method: 'POST'
      });
      setPresetStatus('Preset recalled', 'success');
    } catch (error) {
      console.error('Failed to recall preset', error);
      setPresetStatus(parseErrorMessage(error), 'error');
    }
  }

  async function updatePresetFromCurrent(presetId) {
    try {
      await fetchJSON(`/api/presets/${encodeURIComponent(presetId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ use_current: true })
      });
      setPresetStatus('Preset updated from current values', 'success');
      await loadPresets();
    } catch (error) {
      console.error('Failed to update preset', error);
      setPresetStatus(parseErrorMessage(error), 'error');
    }
  }

  async function deletePreset(presetId) {
    const preset = getPresetById(presetId);
    const label = preset && preset.name ? preset.name : 'this preset';
    if (!window.confirm(`Delete ${label}?`)) return;
    try {
      await fetchJSON(`/api/presets/${encodeURIComponent(presetId)}`, {
        method: 'DELETE'
      });
      setPresetStatus('Preset deleted', 'success');
      await loadPresets();
    } catch (error) {
      console.error('Failed to delete preset', error);
      setPresetStatus(parseErrorMessage(error), 'error');
    }
  }

  async function renamePreset(presetId) {
    const preset = getPresetById(presetId);
    const currentName = preset && preset.name ? preset.name : '';
    const newName = window.prompt('Enter a new name for this preset:', currentName);
    if (newName === null) return;
    const trimmed = newName.trim();
    if (!trimmed) {
      setPresetStatus('Preset name cannot be empty', 'error');
      return;
    }
    if (trimmed === currentName) {
      return;
    }
    try {
      await fetchJSON(`/api/presets/${encodeURIComponent(presetId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: trimmed })
      });
      setPresetStatus('Preset renamed', 'success');
      await loadPresets();
    } catch (error) {
      console.error('Failed to rename preset', error);
      setPresetStatus(parseErrorMessage(error), 'error');
    }
  }

  async function assignPresetButton(presetId, value) {
    const existing = getButtonForPreset(presetId);
    const trimmed = String(value ?? '').trim();

    if (trimmed === '') {
      if (!existing) {
        return;
      }
      try {
        await fetchJSON('/api/preset-buttons', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ button: existing.button, preset_id: '' })
        });
        setPresetStatus('Preset button cleared', 'success');
        await loadPresets();
      } catch (error) {
        console.error('Failed to clear preset button', error);
        setPresetStatus(parseErrorMessage(error), 'error');
        await loadPresets();
      }
      return;
    }

    const parsed = Number(trimmed);
    if (!Number.isInteger(parsed)) {
      setPresetStatus('Button must be an integer', 'error');
      await loadPresets();
      return;
    }
    if (existing && Number(existing.button) === parsed) {
      return;
    }

    try {
      await fetchJSON('/api/preset-buttons', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ button: parsed, preset_id: presetId })
      });
      setPresetStatus(`Assigned button ${parsed}`, 'success');
      await loadPresets();
    } catch (error) {
      console.error('Failed to assign preset button', error);
      setPresetStatus(parseErrorMessage(error), 'error');
      await loadPresets();
    }
  }

  function showCaptureError(message) {
    const el = document.getElementById('pcap-error');
    if (!el) return;
    if (message) {
      el.textContent = message;
      el.style.display = 'block';
    } else {
      el.textContent = '';
      el.style.display = 'none';
    }
  }

  function updateCaptureUI(state) {
    CAPTURE_STATE = state || null;

    const select = document.getElementById('pcap-interface');
    const startBtn = document.getElementById('pcap-start');
    const stopBtn = document.getElementById('pcap-stop');
    const downloadLink = document.getElementById('pcap-download');
    const statusEl = document.getElementById('pcap-status');

    const active = !!(CAPTURE_STATE && CAPTURE_STATE.active);
    const iface = CAPTURE_STATE && CAPTURE_STATE.interface;
    const size = CAPTURE_STATE ? Number(CAPTURE_STATE.bytes_captured || 0) : 0;

    renderCaptureInterfaceOptions(iface);

    if (startBtn) {
      const ready = select && select.value;
      startBtn.disabled = active || !ready;
    }

    if (stopBtn) {
      stopBtn.disabled = !active;
    }

    if (downloadLink) {
      if (CAPTURE_STATE && CAPTURE_STATE.download_ready) {
        downloadLink.style.display = 'inline-block';
        downloadLink.href = `/api/capture/download?ts=${Date.now()}`;
      } else {
        downloadLink.style.display = 'none';
        downloadLink.href = '#';
      }
    }

    if (statusEl) {
      let text = 'Idle';
      if (active) {
        text = `Capturing on ${iface || 'selected interface'}`;
        if (CAPTURE_STATE && CAPTURE_STATE.started_at) {
          try {
            const started = new Date(CAPTURE_STATE.started_at);
            const seconds = (Date.now() - started.getTime()) / 1000;
            text += ` • ${formatDuration(seconds)}`;
          } catch (_err) {
            /* ignore invalid date */
          }
        }
        if (size > 0) {
          text += ` • ${formatBytes(size)}`;
        }
      } else if (CAPTURE_STATE && CAPTURE_STATE.download_ready) {
        text = `Capture ready (${formatBytes(size)})`;
        if (iface) text += ` from ${iface}`;
      } else if (iface) {
        text = `Last capture on ${iface}`;
      }
      statusEl.textContent = text;
    }

    if (CAPTURE_STATE && CAPTURE_STATE.error) {
      showCaptureError(CAPTURE_STATE.error);
    } else {
      showCaptureError('');
    }
  }

  async function refreshCaptureState() {
    try {
      await ensureNetworkAdapters();
      const state = await fetchJSON('/api/capture/status');
      updateCaptureUI(state);
    } catch (error) {
      console.error('Failed to refresh capture state', error);
    }
  }

  async function startPacketCapture() {
    const select = document.getElementById('pcap-interface');
    const startBtn = document.getElementById('pcap-start');
    if (!select || !select.value) {
      showCaptureError('Select an interface before starting a capture.');
      if (startBtn) startBtn.disabled = false;
      return;
    }

    showCaptureError('');
    if (startBtn) startBtn.disabled = true;

    try {
      const state = await fetchJSON('/api/capture/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ interface: select.value })
      });
      updateCaptureUI(state);
    } catch (error) {
      const message = parseErrorMessage(error);
      const state = Object.assign({}, CAPTURE_STATE || {});
      state.error = message;
      updateCaptureUI(state);
    } finally {
      if (startBtn) {
        const ready = select && select.value;
        const active = CAPTURE_STATE && CAPTURE_STATE.active;
        startBtn.disabled = !!active || !ready;
      }
    }
  }

  async function stopPacketCapture() {
    const stopBtn = document.getElementById('pcap-stop');
    if (stopBtn) stopBtn.disabled = true;

    try {
      const state = await fetchJSON('/api/capture/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      });
      updateCaptureUI(state);
    } catch (error) {
      const message = parseErrorMessage(error);
      const state = Object.assign({}, CAPTURE_STATE || {});
      state.error = message;
      updateCaptureUI(state);
    } finally {
      if (stopBtn) {
        const active = CAPTURE_STATE && CAPTURE_STATE.active;
        stopBtn.disabled = !active;
      }
    }
  }

  /* ------------------------------------------------------------------------ */
  /* Tab controls                                                             */
  /* ------------------------------------------------------------------------ */

  function setActiveTab(tab) {
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    document.querySelectorAll('.tab-panel').forEach((panel) => {
      panel.classList.toggle('active', panel.dataset.tab === tab);
    });
    try {
      localStorage.setItem(TAB_STORAGE_KEY, tab);
    } catch (_error) {
      /* ignore storage errors */
    }
  }

  function initTabs() {
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => setActiveTab(btn.dataset.tab));
    });

    let initial = 'dashboard';
    try {
      const stored = localStorage.getItem(TAB_STORAGE_KEY);
      if (stored && document.querySelector(`.tab-btn[data-tab="${stored}"]`)) {
        initial = stored;
      }
    } catch (_error) {
      /* ignore storage errors */
    }
    setActiveTab(initial);
  }

  /* ------------------------------------------------------------------------ */
  /* Status + indicator helpers                                               */
  /* ------------------------------------------------------------------------ */

  function setPill(id, ok, off = false) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'pill ' + (off ? 'off' : ok ? 'ok' : 'err');
  }

  function setLed(id, on) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.toggle('on', !!on);
  }

  function updateFixtureLeds(list) {
    const rows = document.querySelectorAll('#fixture-led-bank .led');
    rows.forEach((row, index) => {
      const data = Array.isArray(list) ? list[index] : null;
      const bulb = row.querySelector('.led-bulb');
      const label = row.querySelector('.led-label');
      const on = data && !!data.on;
      if (bulb) bulb.classList.toggle('on', on);
      if (label) label.textContent = data && data.label ? data.label : `Slot ${index + 1}`;
    });
  }

  async function refreshStatus() {
    try {
      const status = await fetchJSON('/api/status');
      document.getElementById('joy-name').innerText = status.joystick_name || (status.virtual ? 'Virtual HOTAS' : '-');
      document.getElementById('joy-axes').innerText = status.axes;
      document.getElementById('joy-buttons').innerText = status.buttons;
      document.getElementById('last-frame').innerText = status.last_frame || '-';

      setPill('status-pill', status.active, !status.active);
      document.getElementById('status-text').innerText = status.active ? 'Active' : 'Idle';

      const joystickMissing = status.joystick_name === '' && !status.error;
      setPill('health-pill', !status.error, joystickMissing);
      document.getElementById('health-text').innerText = status.error ? `Error: ${status.error_msg}` : 'Good';

      setLed('led-power', !!status.power_led);
      setLed('led-error', !!status.error_led);
      updateFixtureLeds(status.fixture_leds || []);

      const logsResponse = await fetch('/api/logs');
      const logsText = await logsResponse.text();
      const textarea = document.getElementById('logs');
      textarea.value = logsText;
      textarea.scrollTop = textarea.scrollHeight;
    } catch (error) {
      console.error('Failed to refresh status', error);
    }
  }

  /* ------------------------------------------------------------------------ */
  /* Settings                                                                 */
  /* ------------------------------------------------------------------------ */

  async function loadSettings() {
    const data = await fetchJSON('/api/settings');
    const form = document.getElementById('settings-form');
    if (!form) return;

    await refreshUsbDevices(typeof data.usb_device === 'string' ? data.usb_device : '');

    Object.keys(data).forEach((key) => {
      const el = form[key];
      if (!el) return;

      if (isCheckbox(el)) {
        el.checked = !!data[key];
        return;
      }

      if (Array.isArray(data[key])) {
        el.value = data[key].join(', ');
        return;
      }

      el.value = data[key];
      if (el.tagName === 'SELECT') {
        const target = String(data[key] ?? '');
        let matched = false;
        for (const opt of el.options) {
          if (opt.value === target) {
            matched = true;
            break;
          }
        }
        if (!matched && el.options.length) {
          el.value = el.options[0].value;
        }
      }
    });

    const sacnSelected = Array.isArray(data.sacn_bind_addresses) ? data.sacn_bind_addresses : [];
    await refreshNetworkAdapters(sacnSelected);
    const sacnHidden = document.getElementById('sacn_bind_addresses');
    if (sacnHidden) {
      sacnHidden.value = JSON.stringify(sacnSelected);
    }

    if (form.button_actions) {
      try {
        form.button_actions.value = JSON.stringify(data.button_actions || [], null, 2);
      } catch (_error) {
        form.button_actions.value = '[]';
      }
    }

    const throttleInvert = !!data.virtual_throttle_invert;
    const throttle = document.getElementById('vth');
    if (throttle) throttle.dataset.invert = String(throttleInvert);

    readBtnIndicesFromForm();
    await vjoySyncEnabled();
  }

  async function saveSettings() {
    syncFixtureCompat();
    syncSacnInterfaces();

    const form = document.getElementById('settings-form');
    if (!form) return;

    const payload = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      payload[el.name] = isCheckbox(el) ? el.checked : el.value;
    }

    if ('gpio_fixture_led_pins' in payload) {
      const pins = String(payload.gpio_fixture_led_pins || '')
        .split(',')
        .map((pin) => pin.trim())
        .filter((pin) => pin.length)
        .map((pin) => Number(pin))
        .filter((pin) => Number.isInteger(pin));
      payload.gpio_fixture_led_pins = pins;
    }

    if (typeof payload.sacn_bind_addresses === 'string') {
      try {
        const parsed = JSON.parse(payload.sacn_bind_addresses);
        payload.sacn_bind_addresses = Array.isArray(parsed) ? parsed : [];
      } catch (_error) {
        payload.sacn_bind_addresses = [];
      }
    }

    const resp = await fetchJSON('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    alert((resp && resp.message) || 'Saved');
    await loadFixtures();
    await loadSettings();
  }

  /* ------------------------------------------------------------------------ */
  /* Restart service confirmation                                             */
  /* ------------------------------------------------------------------------ */

  async function restartService(btn) {
    if (!btn) return;

    if (btn.dataset.confirm === 'true') {
      btn.disabled = true;
      btn.textContent = 'Restarting...';
      btn.classList.add('danger');
      clearTimeout(restartConfirmTimer);
      restartConfirmTimer = null;

      try {
        const resp = await fetchJSON('/api/restart', { method: 'POST' });
        alert((resp && resp.message) || 'Restarting service...');
      } catch (error) {
        alert(error.message || error);
        btn.disabled = false;
        btn.textContent = 'Restart Service';
        btn.classList.add('danger');
        btn.dataset.confirm = '';
        return;
      }

      return;
    }

    btn.dataset.confirm = 'true';
    btn.textContent = 'Click again to confirm';
    btn.classList.add('danger');
    clearTimeout(restartConfirmTimer);
    restartConfirmTimer = setTimeout(() => {
      btn.dataset.confirm = '';
      btn.textContent = 'Restart Service';
      btn.disabled = false;
    }, 5000);
  }

  /* ------------------------------------------------------------------------ */
  /* Fixtures                                                                 */
  /* ------------------------------------------------------------------------ */

  function syncFixtureCompat() {
    const enabled = document.getElementById('fx_enabled');
    const invertPan = document.getElementById('fx_invert_pan');
    const invertTilt = document.getElementById('fx_invert_tilt');
    const enabledHidden = document.getElementById('fx_enabled_hidden');
    const panHidden = document.getElementById('fx_invert_pan_hidden');
    const tiltHidden = document.getElementById('fx_invert_tilt_hidden');

    if (enabled && enabledHidden) {
      enabledHidden.value = enabled.checked ? 'True' : 'False';
    }
    if (invertPan && panHidden) {
      panHidden.value = invertPan.checked ? 'True' : 'False';
    }
    if (invertTilt && tiltHidden) {
      tiltHidden.value = invertTilt.checked ? 'True' : 'False';
    }
  }

  async function activate() {
    await fetchJSON('/api/activate', { method: 'POST' });
  }

  async function release() {
    await fetchJSON('/api/release', { method: 'POST' });
  }

  function showImport() {
    const area = document.getElementById('import-area');
    if (area) area.style.display = 'block';
  }

  function hideImport() {
    const area = document.getElementById('import-area');
    if (area) area.style.display = 'none';
  }

  async function doImport() {
    const textarea = document.getElementById('csvtext');
    if (!textarea) return;

    try {
      await fetchJSON('/api/fixtures/import', {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain' },
        body: textarea.value
      });
      hideImport();
      await loadFixtures();
    } catch (error) {
      alert(error.message || error);
    }
  }

  async function loadFixtures() {
    const data = await fetchJSON('/api/fixtures');

    const multiUniverse = document.getElementById('multi-universe');
    if (multiUniverse) {
      multiUniverse.checked = !!data.multi_universe_enabled;
    }

    const form = document.getElementById('fx-form');
    const addBtn = document.getElementById('fx-add-btn');
    const limitMsg = document.getElementById('fixture-limit-msg');
    const count = Array.isArray(data.fixtures) ? data.fixtures.length : 0;
    const remaining = Math.max(0, FIXTURE_LIMIT - count);

    if (form) form.dataset.remaining = String(remaining);
    if (addBtn) addBtn.disabled = remaining <= 0;

    if (limitMsg) {
      if (remaining <= 0) {
        limitMsg.textContent = `Fixture limit reached (${FIXTURE_LIMIT}). Delete one to add another.`;
      } else if (remaining === 1) {
        limitMsg.textContent = 'You can add 1 more fixture.';
      } else {
        limitMsg.textContent = `You can add ${remaining} more fixtures.`;
      }
    }

    const wrap = document.getElementById('fixture-list');
    if (!wrap) return;
    wrap.innerHTML = '';

    if (!count) {
      wrap.innerHTML = '<small>No fixtures yet.</small>';
      return;
    }

    data.fixtures.slice(0, FIXTURE_LIMIT).forEach((fixture) => {
      const card = document.createElement('div');
      card.className = 'fixture-card';
      card.innerHTML = `
        <div><b>${fixture.id}</b> ${fixture.enabled ? '<span class="badge ok">Enabled</span>' : '<span class="badge warn">Disabled</span>'}</div>
        <div class="small muted">Uni ${fixture.universe} • Pan ${fixture.pan_coarse}/${fixture.pan_fine || 0} • Tilt ${fixture.tilt_coarse}/${fixture.tilt_fine || 0} • Dim ${fixture.dimmer || 0} • Zoom ${fixture.zoom || 0}${fixture.zoom_fine ? (`/${fixture.zoom_fine}`) : ''}${colorTempSummary(fixture)}</div>
        <div class="small muted">Invert P:${fixture.invert_pan ? 'Y' : 'N'} T:${fixture.invert_tilt ? 'Y' : 'N'} • Bias P:${fixture.pan_bias || 0} T:${fixture.tilt_bias || 0}${statusLedSummary(fixture)}</div>
        <details class="fixture-details">
          <summary>Edit</summary>
          <div class="fxgrid">
            ${editInput('Enabled','enabled',fixture.enabled)}
            ${editInput('Universe','universe',fixture.universe,'number')}
            ${editInput('Start Addr','start_addr',fixture.start_addr,'number')}
            ${editInput('Pan Coarse','pan_coarse',fixture.pan_coarse,'number')}
            ${editInput('Pan Fine','pan_fine',fixture.pan_fine,'number')}
            ${editInput('Tilt Coarse','tilt_coarse',fixture.tilt_coarse,'number')}
            ${editInput('Tilt Fine','tilt_fine',fixture.tilt_fine,'number')}
            ${editInput('Dimmer','dimmer',fixture.dimmer,'number')}
            ${editInput('Zoom','zoom',fixture.zoom,'number')}
            ${editInput('Zoom Fine','zoom_fine',fixture.zoom_fine,'number')}
            ${editInput('Color Temp Ch','color_temp_channel',fixture.color_temp_channel,'number')}
            ${editInput('Color Temp Val','color_temp_value',fixture.color_temp_value,'number')}
            ${editInput('Invert Pan','invert_pan',fixture.invert_pan)}
            ${editInput('Invert Tilt','invert_tilt',fixture.invert_tilt)}
            ${editInput('Pan Bias','pan_bias',fixture.pan_bias,'number')}
            ${editInput('Tilt Bias','tilt_bias',fixture.tilt_bias,'number')}
            ${editInput('Status LED','status_led',fixture.status_led,'number')}
          </div>
          <div class="form-actions">
            <button class="btn primary" onclick="saveFixture('${fixture.id}', this.closest('.form-actions').previousElementSibling)">Save</button>
            <button class="btn" onclick="toggleFixture('${fixture.id}', ${!fixture.enabled})">${fixture.enabled ? 'Disable' : 'Enable'}</button>
            <button class="btn danger" onclick="deleteFixture('${fixture.id}')">Delete</button>
          </div>
        </details>`;
      wrap.appendChild(card);
    });
  }

  function editInput(label, name, value, type) {
    if (type === 'number') {
      const safe = value ?? '';
      return `<div><label>${label}</label><input type="number" name="${name}" value="${safe}"></div>`;
    }

    const raw = value === undefined || value === null ? '' : String(value);
    const rawLower = raw.toLowerCase();
    const boolish = (typeof value === 'boolean') || ['true', 'false', '1', '0', 'yes', 'no', 'on', 'off', ''].includes(rawLower);

    if (boolish) {
      const truthy = ['1', 'true', 'yes', 'on'];
      const boolVal = typeof value === 'boolean' ? value : truthy.includes(rawLower);
      return `<div><label>${label}</label><select name="${name}"><option value="True"${boolVal ? ' selected' : ''}>True</option><option value="False"${!boolVal ? ' selected' : ''}>False</option></select></div>`;
    }

    return `<div><label>${label}</label><input type="text" name="${name}" value="${raw}"></div>`;
  }

  function colorTempSummary(fixture) {
    const channel = Number(fixture.color_temp_channel || 0);
    if (channel > 0) {
      const raw = fixture.color_temp_value;
      const value = raw === undefined || raw === null || raw === '' ? '' : `=${raw}`;
      return ` • Color Temp ${channel}${value}`;
    }
    return '';
  }

  function statusLedSummary(fixture) {
    const led = Number(fixture.status_led || 0);
    if (led > 0) {
      return ` • Status LED ${led}`;
    }
    return '';
  }

  async function toggleFixture(id, enabled) {
    await fetchJSON(`/api/fixtures/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled })
    });
    await loadFixtures();
  }

  async function deleteFixture(id) {
    if (!confirm(`Delete fixture ${id}?`)) return;
    await fetchJSON(`/api/fixtures/${encodeURIComponent(id)}`, { method: 'DELETE' });
    await loadFixtures();
  }

  async function saveFixture(id, gridEl) {
    if (!gridEl) return;
    const fields = {};
    for (const el of gridEl.querySelectorAll('input, select')) {
      fields[el.name] = el.value;
    }
    await fetchJSON(`/api/fixtures/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(fields)
    });
    await loadFixtures();
  }

  async function addFixture() {
    const form = document.getElementById('fx-form');
    if (!form) return;

    const remaining = Number(form.dataset.remaining || '0');
    if (remaining <= 0) {
      alert(`Fixture limit of ${FIXTURE_LIMIT} reached. Delete a fixture before adding another.`);
      return;
    }

    syncFixtureCompat();

    const payload = {};
    for (const el of form.elements) {
      if (el.name) payload[el.name] = el.value;
    }

    try {
      await fetchJSON('/api/fixtures', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    } catch (error) {
      alert(error.message || error);
      return;
    }

    form.reset();

    const enabled = document.getElementById('fx_enabled');
    const invertPan = document.getElementById('fx_invert_pan');
    const invertTilt = document.getElementById('fx_invert_tilt');
    if (enabled) enabled.checked = true;
    if (invertPan) invertPan.checked = false;
    if (invertTilt) invertTilt.checked = false;

    if (form.enabled) form.enabled.value = 'True';
    if (form.invert_pan) form.invert_pan.value = 'False';
    if (form.invert_tilt) form.invert_tilt.value = 'False';
    if (form.status_led) form.status_led.value = '';

    await loadFixtures();
  }

  async function toggleMU() {
    const checkbox = document.getElementById('multi-universe');
    if (!checkbox) return;
    await fetchJSON('/api/fixtures/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ multi_universe_enabled: checkbox.checked })
    });
    await loadFixtures();
  }

  /* ------------------------------------------------------------------------ */
  /* Virtual HOTAS                                                            */
  /* ------------------------------------------------------------------------ */

  function readBtnIndicesFromForm() {
    const form = document.getElementById('settings-form');
    const get = (key) => {
      if (form && form[key]) {
        return parseInt(form[key].value || '0', 10) || 0;
      }
      return 0;
    };
    BTN_ACTIVATE = get('btn_activate');
    BTN_RELEASE = get('btn_release');
    BTN_FLASH10 = get('btn_flash10');
    BTN_DIMOFF = get('btn_dim_off');
    BTN_FINE = get('btn_fine');
    BTN_ZOOM = get('btn_zoom_mod');
  }

  async function vjoySyncEnabled() {
    const state = await fetchJSON('/api/virtual');
    const checkbox = document.getElementById('vjoy-en');
    if (checkbox) checkbox.checked = !!state.enabled;
    setPadDot(state.x, state.y);
    document.getElementById('vx').innerText = Number(state.x).toFixed(2);
    document.getElementById('vy').innerText = Number(state.y).toFixed(2);
    const throttle = document.getElementById('vth');
    if (throttle) throttle.value = Math.round((state.throttle + 1) * 50);
    const zoom = document.getElementById('vzoom');
    if (zoom) zoom.value = Math.round((state.zaxis || 0) * 100);
  }

  async function vjoyEnable(on) {
    await fetchJSON('/api/virtual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: on })
    });
  }

  function setPadDot(x, y) {
    const pad = document.getElementById('pad');
    const dot = document.getElementById('pad-dot');
    if (!pad || !dot) return;

    const width = pad.clientWidth;
    const height = pad.clientHeight;
    const cx = (x * 0.5 + 0.5) * width;
    const cy = (1 - (y * 0.5 + 0.5)) * height;
    dot.style.left = `${cx}px`;
    dot.style.top = `${cy}px`;
  }

  function padSend(x, y) {
    document.getElementById('vx').innerText = x.toFixed(2);
    document.getElementById('vy').innerText = y.toFixed(2);
    fetch('/api/virtual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x, y })
    }).catch(() => {
      /* ignore network errors */
    });
  }

  function padPointToXY(ev) {
    const pad = document.getElementById('pad');
    const rect = pad.getBoundingClientRect();
    const px = Math.max(0, Math.min(rect.width, ev.clientX - rect.left));
    const py = Math.max(0, Math.min(rect.height, ev.clientY - rect.top));
    const x = (px / rect.width) * 2 - 1;
    const y = -((py / rect.height) * 2 - 1);
    return {
      x: Math.max(-1, Math.min(1, x)),
      y: Math.max(-1, Math.min(1, y))
    };
  }

  function padCenter() {
    const x = 0;
    const y = 0;
    setPadDot(x, y);
    padSend(x, y);
  }

  function initPad() {
    const pad = document.getElementById('pad');
    if (!pad) return;

    let pointerId = null;
    let isDown = false;

    pad.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      isDown = true;
      pointerId = ev.pointerId;
      try {
        pad.setPointerCapture(pointerId);
      } catch (_error) {
        /* ignore */
      }
      const { x, y } = padPointToXY(ev);
      setPadDot(x, y);
      padSend(x, y);
    });

    pad.addEventListener('pointermove', (ev) => {
      if (!isDown) return;
      ev.preventDefault();
      const { x, y } = padPointToXY(ev);
      setPadDot(x, y);
      padSend(x, y);
    });

    const end = () => {
      if (!isDown) return;
      isDown = false;
      try {
        pad.releasePointerCapture(pointerId);
      } catch (_error) {
        /* ignore */
      }
      pointerId = null;
      padCenter();
    };

    pad.addEventListener('pointerup', end);
    pad.addEventListener('pointercancel', end);
    pad.addEventListener('pointerleave', end);

    setPadDot(0, 0);
  }

  function initZoomSlider() {
    const zoom = document.getElementById('vzoom');
    if (!zoom) return;

    let engaged = false;
    let pointerId = null;

    const centerZoom = () => {
      if (!engaged) return;
      engaged = false;
      if (pointerId !== null) {
        try {
          zoom.releasePointerCapture(pointerId);
        } catch (_error) {
          /* ignore */
        }
        pointerId = null;
      }
      zoom.value = '0';
      vjoyZoom(0);
    };

    zoom.addEventListener('pointerdown', (ev) => {
      engaged = true;
      pointerId = ev.pointerId;
      try {
        zoom.setPointerCapture(pointerId);
      } catch (_error) {
        /* ignore */
      }
    });

    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((eventName) => {
      zoom.addEventListener(eventName, centerZoom);
    });

    zoom.addEventListener('pointerleave', (ev) => {
      if (!ev.buttons) centerZoom();
    });

    zoom.addEventListener('keydown', () => {
      engaged = true;
    });
    zoom.addEventListener('keyup', centerZoom);
    zoom.addEventListener('blur', centerZoom);
  }

  function vjoyThrottle(val) {
    const slider = document.getElementById('vth');
    if (!slider) return;
    const invert = slider.dataset.invert === 'true';
    const numeric = parseFloat(val);
    const axis = invert ? numeric / 50 - 1.0 : 1.0 - numeric / 50;
    fetch('/api/virtual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ throttle: axis })
    });
  }

  function vjoyZoom(val) {
    const axis = Math.max(-1, Math.min(1, parseFloat(val) / 100));
    fetch('/api/virtual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ zaxis: axis })
    });
  }

  async function vpress(button) {
    await fetchJSON('/api/virtual/press', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ button })
    });
  }

  async function vrelease(button) {
    await fetchJSON('/api/virtual/release', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ button })
    });
  }

  /* ------------------------------------------------------------------------ */
  /* Initialization                                                           */
  /* ------------------------------------------------------------------------ */

  async function initialize() {
    initTabs();
    initPad();
    initZoomSlider();

    await loadSettings();
    await loadFixtures();
    await loadPresets();

    refreshStatus();
    setInterval(refreshStatus, 1000);

    refreshCaptureState();
    if (!CAPTURE_POLL_TIMER) {
      CAPTURE_POLL_TIMER = setInterval(refreshCaptureState, 5000);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, { once: true });
  } else {
    initialize();
  }

  Object.assign(window, {
    activate,
    release,
    showImport,
    hideImport,
    doImport,
    saveSettings,
    restartService,
    addFixture,
    toggleFixture,
    deleteFixture,
    saveFixture,
    toggleMU,
    startPacketCapture,
    stopPacketCapture,
    savePresetFromCurrent,
    refreshPresetState,
    recallPreset,
    updatePresetFromCurrent,
    renamePreset,
    deletePreset,
    assignPresetButton,
    refreshUsbDevices,
    vjoyEnable,
    vjoyThrottle,
    vjoyZoom,
    vpress,
    vrelease
  });

  Object.defineProperties(window, {
    BTN_ACTIVATE: { get: () => BTN_ACTIVATE },
    BTN_RELEASE: { get: () => BTN_RELEASE },
    BTN_FLASH10: { get: () => BTN_FLASH10 },
    BTN_DIMOFF: { get: () => BTN_DIMOFF },
    BTN_FINE: { get: () => BTN_FINE },
    BTN_ZOOM: { get: () => BTN_ZOOM }
  });
})();

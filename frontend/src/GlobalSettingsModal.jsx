import React, { useEffect, useRef, useState } from 'react';
import { AlertCircle, RefreshCw, X } from 'lucide-react';

const requestError = (data, fallback) => typeof data.detail === 'string' ? data.detail : fallback;

export default function GlobalSettingsModal({ onClose }) {
  const dialogRef = useRef(null);
  const inputRef = useRef(null);
  const saveInFlight = useRef(false);
  const [concurrentRuns, setConcurrentRuns] = useState('');
  const [bounds, setBounds] = useState({ min: 1, max: 32 });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [validationError, setValidationError] = useState('');
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const previouslyFocused = document.activeElement;
    const dialog = dialogRef.current;
    dialog.showModal();
    return () => {
      dialog.close();
      if (previouslyFocused instanceof HTMLElement && previouslyFocused.isConnected) {
        previouslyFocused.focus();
      }
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError('');
    const load = async () => {
      try {
        const response = await fetch('/api/settings', { signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw new Error(requestError(data, 'Could not load global settings.'));
        if (controller.signal.aborted) return;
        setBounds({ min: data.min_concurrent_runs, max: data.max_concurrent_runs });
        setConcurrentRuns(String(data.concurrent_runs));
      } catch (error) {
        if (!controller.signal.aborted) setLoadError(error.message || 'Could not load global settings.');
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    load();
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!loading && !loadError) inputRef.current?.focus();
  }, [loading, loadError]);

  const dismiss = () => {
    if (!saveInFlight.current) onClose();
  };

  const save = async (event) => {
    event.preventDefault();
    if (loading || loadError || saveInFlight.current) return;
    const value = Number(concurrentRuns);
    if (!/^\d+$/.test(concurrentRuns) || !Number.isInteger(value) || value < bounds.min || value > bounds.max) {
      setValidationError(`Enter a whole number from ${bounds.min} to ${bounds.max}.`);
      inputRef.current?.focus();
      return;
    }
    setValidationError('');
    setSaveError('');
    saveInFlight.current = true;
    setSaving(true);
    try {
      const response = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ concurrent_runs: value }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(requestError(data, 'Could not save global settings. Please try again.'));
      onClose();
    } catch (error) {
      setSaveError(error.message || 'Could not save global settings. Please try again.');
    } finally {
      saveInFlight.current = false;
      setSaving(false);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="global-settings-modal"
      aria-labelledby="global-settings-title"
      aria-describedby="global-settings-description"
      onCancel={(event) => {
        event.preventDefault();
        dismiss();
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dismiss();
      }}
    >
      <button type="button" className="modal-close" onClick={dismiss} disabled={saving} aria-label="Close global settings"><X size={18} /></button>
      <h2 id="global-settings-title">Global settings</h2>
      <p id="global-settings-description" className="settings-description">Applies to queued study-set artifacts. Active runs finish before a lower limit takes effect. Settings are saved across application restarts.</p>
      {loading ? (
        <p className="settings-loading" role="status"><RefreshCw size={16} className="spinner" /> Loading settings…</p>
      ) : loadError ? (
        <>
          <p className="settings-error" role="alert"><AlertCircle size={16} /> {loadError}</p>
          <div className="modal-actions"><button type="button" className="btn-secondary" onClick={dismiss}>Cancel</button><button type="button" className="btn-secondary" onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={16} /> Retry</button></div>
        </>
      ) : (
        <form onSubmit={save} noValidate aria-busy={saving}>
          <div className="settings-field">
            <label htmlFor="concurrent-runs" className="form-label">Concurrent runs</label>
            <input
              ref={inputRef}
              id="concurrent-runs"
              className="form-input"
              type="number"
              inputMode="numeric"
              min={bounds.min}
              max={bounds.max}
              step="1"
              value={concurrentRuns}
              disabled={saving}
              aria-invalid={!!validationError}
              aria-describedby={`concurrent-runs-hint${validationError ? ' concurrent-runs-error' : ''}`}
              onChange={(event) => {
                setConcurrentRuns(event.target.value);
                setValidationError('');
                setSaveError('');
              }}
            />
          </div>
          <p id="concurrent-runs-hint" className="settings-hint">Choose {bounds.min}–{bounds.max} concurrent runs.</p>
          {validationError && <p id="concurrent-runs-error" className="settings-error" role="alert">{validationError}</p>}
          {saveError && <p className="settings-error" role="alert"><AlertCircle size={16} /> {saveError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn-secondary" onClick={dismiss} disabled={saving}>Cancel</button>
            <button type="submit" className="btn-secondary btn-settings-save" disabled={saving}>{saving ? <><RefreshCw size={16} className="spinner" /> Saving…</> : 'OK'}</button>
          </div>
        </form>
      )}
    </dialog>
  );
}

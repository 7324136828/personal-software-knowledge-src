import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle, Archive, ChevronDown, ChevronRight, Clock3, Download,
  FileText, GripVertical, RefreshCw, RotateCcw, Trash2, X,
} from 'lucide-react';

const DATE_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

function formatDate(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : DATE_FORMAT.format(date);
}

async function responseError(response, fallback) {
  try {
    const body = await response.json();
    return body.detail || fallback;
  } catch {
    return fallback;
  }
}

function groupStatus(items) {
  if (items.some((item) => item.status === 'in_progress')) return 'in_progress';
  if (items.some((item) => item.status === 'queued')) return 'queued';
  if (items.some((item) => item.status === 'failed')) return 'failed';
  if (items.some((item) => item.status === 'discarding')) return 'discarding';
  return 'completed';
}

function hasOutput(artifact) {
  return artifact.output_available ?? (artifact.status === 'completed' && Boolean(artifact.download_url));
}

function canDeleteGroup(group) {
  return group.items.some((item) => item.can_discard ?? (item.status !== 'discarding'));
}

function downloadFilename(response, fallback) {
  const disposition = response.headers.get('Content-Disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) {
    try { return decodeURIComponent(encoded[1]); } catch { /* Use the plain filename or fallback. */ }
  }
  return disposition.match(/filename="([^"]+)"/i)?.[1] || fallback;
}

function saveDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function HistoryView() {
  const [conversions, setConversions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [historyError, setHistoryError] = useState(null);
  const [expanded, setExpanded] = useState({});
  const [busyId, setBusyId] = useState(null);
  const [retryingId, setRetryingId] = useState(null);
  const [groupAction, setGroupAction] = useState(null);
  const [downloadGroupId, setDownloadGroupId] = useState(null);
  const [selectedGroupIds, setSelectedGroupIds] = useState([]);
  const [draggedGroup, setDraggedGroup] = useState(null);
  const [retryArtifact, setRetryArtifact] = useState(null);
  const [chunkCharacters, setChunkCharacters] = useState('2048');
  const [retryApiKey, setRetryApiKey] = useState('');
  const historyRequest = useRef(null);
  const actionBusy = busyId !== null || groupAction !== null || downloadGroupId !== null;

  const groups = useMemo(() => {
    const grouped = new Map();
    conversions.forEach((item) => {
      if (!grouped.has(item.source_group_id)) {
        grouped.set(item.source_group_id, {
          id: item.source_group_id, filename: item.input_filename,
          created_at: item.created_at, priority: item.priority, items: [],
        });
      }
      grouped.get(item.source_group_id).items.push(item);
    });
    return Array.from(grouped.values()).map((group) => ({ ...group, status: groupStatus(group.items) }));
  }, [conversions]);

  const selectableGroups = useMemo(() => groups.filter(canDeleteGroup), [groups]);
  const selectedGroups = useMemo(() => {
    const selectedIds = new Set(selectedGroupIds);
    return selectableGroups.filter((group) => selectedIds.has(group.id));
  }, [selectableGroups, selectedGroupIds]);
  const selectedDownloadableGroups = useMemo(() => selectedGroups.filter((group) => group.items.some(hasOutput)), [selectedGroups]);

  useEffect(() => {
    const availableIds = new Set(selectableGroups.map((group) => group.id));
    setSelectedGroupIds((current) => {
      const next = current.filter((id) => availableIds.has(id));
      return next.length === current.length ? current : next;
    });
  }, [selectableGroups]);

  const fetchHistory = useCallback(({ quiet = false, force = false } = {}) => {
    if (!quiet) setLoading(true);
    if (historyRequest.current && !force) return historyRequest.current.promise;
    historyRequest.current?.controller.abort();
    const controller = new AbortController();
    const request = { controller, promise: null };
    historyRequest.current = request;
    request.promise = (async () => {
      try {
        const response = await fetch('/api/history', { signal: controller.signal });
        if (!response.ok) throw new Error(await responseError(response, 'Could not load file history.'));
        const data = await response.json();
        if (controller.signal.aborted) return;
        setConversions(data.conversions || []);
        setHistoryError(null);
      } catch (err) {
        if (!controller.signal.aborted) setHistoryError(err.message || 'Could not load file history.');
      } finally {
        if (historyRequest.current === request) {
          historyRequest.current = null;
          if (!controller.signal.aborted) setLoading(false);
        }
      }
    })();
    return request.promise;
  }, []);

  useEffect(() => {
    fetchHistory();
    const timer = window.setInterval(() => fetchHistory({ quiet: true }), 2000);
    return () => {
      window.clearInterval(timer);
      historyRequest.current?.controller.abort();
      historyRequest.current = null;
    };
  }, [fetchHistory]);

  const discardArtifact = async (artifact) => {
    if (!window.confirm(`Delete saved work for ${artifact.action_title}?`)) return;
    setBusyId(artifact.id);
    setError(null);
    try {
      const response = await fetch(`/api/history/${artifact.id}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(await responseError(response, 'Could not delete artifact.'));
      await fetchHistory({ quiet: true, force: true });
    } catch (err) {
      setError(err.message || 'Could not delete artifact.');
    } finally {
      setBusyId(null);
    }
  };

  const cancelGroup = async (group) => {
    if (!window.confirm(`Cancel all unfinished artifacts for ${group.filename}?`)) return;
    setGroupAction({ id: group.id, type: 'cancel' });
    setError(null);
    try {
      const response = await fetch(`/api/history/groups/${encodeURIComponent(group.id)}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(await responseError(response, 'Could not cancel file.'));
      await fetchHistory({ quiet: true, force: true });
    } catch (err) {
      setError(err.message || 'Could not cancel file.');
    } finally {
      setGroupAction(null);
    }
  };

  const deleteGroup = async (group) => {
    if (!window.confirm(`Delete all history, the saved source, and generated files for ${group.filename}? Queued or running artifacts will be canceled. This cannot be undone.`)) return;
    setGroupAction({ id: group.id, type: 'delete' });
    setError(null);
    try {
      const response = await fetch(`/api/history/groups/${encodeURIComponent(group.id)}?include_completed=true`, { method: 'DELETE' });
      if (!response.ok) throw new Error(await responseError(response, 'Could not delete file history.'));
      setSelectedGroupIds((current) => current.filter((id) => id !== group.id));
      setExpanded((current) => {
        const next = { ...current };
        delete next[group.id];
        return next;
      });
      await fetchHistory({ quiet: true, force: true });
    } catch (err) {
      setError(err.message || 'Could not delete file history.');
    } finally {
      setGroupAction(null);
    }
  };

  const deleteSelected = async () => {
    if (actionBusy || selectedGroups.length === 0) return;
    const groupIds = selectedGroups.map((group) => group.id);
    const count = groupIds.length;
    if (!window.confirm(`Delete ${count} selected study ${count === 1 ? 'set' : 'sets'}, including all history, saved sources, and generated files? Queued or running artifacts will be canceled. This cannot be undone.`)) return;
    setGroupAction({ id: 'selected', type: 'delete' });
    setError(null);
    try {
      const response = await fetch('/api/history/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_ids: groupIds }),
      });
      if (!response.ok) throw new Error(await responseError(response, 'Could not delete selected study sets.'));
      const result = await response.json();
      const removedIds = new Set([...(result.deleted_group_ids || []), ...(result.missing_group_ids || [])]);
      setSelectedGroupIds((current) => current.filter((id) => !removedIds.has(id)));
      setExpanded((current) => {
        const next = { ...current };
        removedIds.forEach((id) => { delete next[id]; });
        return next;
      });
      const failedGroups = result.failed_groups || [];
      if (failedGroups.length > 0) {
        setError(`${failedGroups.length} selected study ${failedGroups.length === 1 ? 'set could' : 'sets could'} not be deleted. ${failedGroups[0].error || 'Please try again.'}`);
      }
      await fetchHistory({ quiet: true, force: true });
    } catch (err) {
      setError(err.message || 'Could not delete selected study sets.');
      await fetchHistory({ quiet: true, force: true });
    } finally {
      setGroupAction(null);
    }
  };

  const downloadGroup = async (group) => {
    setDownloadGroupId(group.id);
    setError(null);
    try {
      const response = await fetch(`/api/history/groups/${encodeURIComponent(group.id)}/download`);
      if (!response.ok) throw new Error(await responseError(response, 'Could not download study set.'));
      const blob = await response.blob();
      const stem = group.filename.replace(/\.[^/.]+$/, '') || 'study-set';
      const partial = group.items.some((item) => item.status !== 'completed' || item.output_partial);
      saveDownload(blob, downloadFilename(response, `${stem}-study-set${partial ? '-partial' : ''}.zip`));
    } catch (err) {
      setError(err.message || 'Could not download study set.');
    } finally {
      setDownloadGroupId(null);
    }
  };

  const downloadSelected = async () => {
    const groupIds = selectedDownloadableGroups.map((group) => group.id);
    if (actionBusy || groupIds.length === 0) return;
    setDownloadGroupId('selected');
    setError(null);
    try {
      const response = await fetch('/api/history/download', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_ids: groupIds }),
      });
      if (!response.ok) throw new Error(await responseError(response, 'Could not download selected study sets.'));
      saveDownload(await response.blob(), downloadFilename(response, 'study-sets.zip'));
    } catch (err) {
      setError(err.message || 'Could not download selected study sets.');
    } finally {
      setDownloadGroupId(null);
    }
  };

  const downloadArtifact = async (artifact) => {
    setDownloadGroupId(`artifact:${artifact.id}`);
    setError(null);
    try {
      const response = await fetch(artifact.download_url);
      if (!response.ok) throw new Error(await responseError(response, 'Could not download artifact.'));
      const stem = artifact.input_filename.replace(/\.[^/.]+$/, '') || 'conversion';
      const fallback = `${stem}-${artifact.action}${artifact.output_partial ? '-partial' : ''}.zip`;
      saveDownload(await response.blob(), downloadFilename(response, fallback));
    } catch (err) {
      setError(err.message || 'Could not download artifact.');
    } finally {
      setDownloadGroupId(null);
    }
  };

  const retryConversion = async () => {
    const size = Number(chunkCharacters);
    if (!Number.isInteger(size) || size < 256) {
      setError('Chunk separation must be an integer of at least 256 characters.');
      return;
    }
    const artifact = retryArtifact;
    if (artifact.api_key_was_supplied && !['ollama', 'the_connector'].includes(artifact.connector) && !retryApiKey.trim()) {
      setError('Re-enter the API key used by this artifact. API keys are never stored in history.');
      return;
    }
    setRetryArtifact(null);
    setRetryingId(artifact.id);
    setError(null);
    const formData = new FormData();
    formData.append('chunk_characters', String(size));
    if (retryApiKey.trim()) formData.append('api_key', retryApiKey.trim());
    try {
      const response = await fetch(`/api/history/${artifact.id}/continue`, { method: 'POST', body: formData });
      if (response.status !== 410 && !response.ok) throw new Error(await responseError(response, 'Could not retry artifact.'));
      await fetchHistory({ quiet: true, force: true });
    } catch (err) {
      setError(err.message || 'Could not retry artifact.');
      await fetchHistory({ quiet: true, force: true });
    } finally {
      setRetryingId(null);
      setRetryApiKey('');
    }
  };

  const reorderGroups = async (targetId) => {
    if (actionBusy || !draggedGroup || draggedGroup === targetId) return;
    const ordered = [...groups];
    const from = ordered.findIndex((group) => group.id === draggedGroup);
    const to = ordered.findIndex((group) => group.id === targetId);
    if (from < 0 || to < 0) return;
    const [moved] = ordered.splice(from, 1);
    ordered.splice(to, 0, moved);
    const rank = new Map(ordered.map((group, index) => [group.id, index]));
    setConversions((current) => [...current].sort((a, b) => rank.get(a.source_group_id) - rank.get(b.source_group_id)));
    setDraggedGroup(null);
    setGroupAction({ id: draggedGroup, type: 'order' });
    setError(null);
    try {
      const response = await fetch('/api/history/order', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_ids: ordered.map((group) => group.id) }),
      });
      if (!response.ok) throw new Error(await responseError(response, 'Could not save queue order.'));
      await fetchHistory({ quiet: true, force: true });
    } catch (err) {
      setError(err.message || 'Could not save queue order.');
      await fetchHistory({ quiet: true, force: true });
    } finally {
      setGroupAction(null);
    }
  };

  return (
    <main>
      <div className="history-heading">
        <div><h2 className="page-title"><Archive size={24} /> File History</h2><p className="page-description">Select study sets to download or delete together. Expand a file to manage its artifacts. Drag queued files to change their priority.</p></div>
        <button type="button" className="btn-secondary" onClick={() => fetchHistory()} disabled={loading}><RefreshCw size={16} className={loading ? 'spinner' : ''} /> Refresh</button>
      </div>
      {(error || historyError) && <div className="error-banner" role="alert"><AlertCircle size={20} /><div>{error || historyError}</div>{error && <button type="button" className="remove-btn" aria-label="Dismiss error" onClick={() => setError(null)}><X size={16} /></button>}</div>}
      {groups.length > 0 && <div className="history-selection-toolbar">
        <label className="history-select-all"><input type="checkbox" checked={selectableGroups.length > 0 && selectedGroups.length === selectableGroups.length} ref={(input) => { if (input) input.indeterminate = selectedGroups.length > 0 && selectedGroups.length < selectableGroups.length; }} disabled={selectableGroups.length === 0 || actionBusy} onChange={(event) => setSelectedGroupIds(event.target.checked ? selectableGroups.map((group) => group.id) : [])} /> Select all study sets</label>
        <span aria-live="polite">{selectedGroups.length} selected</span>
        <button type="button" className="btn-secondary btn-download" disabled={selectedDownloadableGroups.length === 0 || actionBusy} onClick={downloadSelected}>{downloadGroupId === 'selected' ? <RefreshCw size={16} className="spinner" /> : <Download size={16} />} Download selected ({selectedDownloadableGroups.length})</button>
        <button type="button" className="btn-secondary btn-danger" disabled={selectedGroups.length === 0 || actionBusy} onClick={deleteSelected}>{groupAction?.id === 'selected' && groupAction.type === 'delete' ? <RefreshCw size={16} className="spinner" /> : <Trash2 size={16} />} Delete selected</button>
        <span className="history-selection-note">Downloads include saved output, including partial output.</span>
      </div>}
      {loading && groups.length === 0 ? (
        <div className="history-empty"><RefreshCw className="spinner" /> Loading history...</div>
      ) : groups.length === 0 ? (
        <div className="history-empty"><FileText size={44} /><strong>No files yet</strong><span>Queued conversions will appear here.</span></div>
      ) : (
        <div className="file-history-list">
          {groups.map((group) => {
            const isOpen = Boolean(expanded[group.id]);
            const unfinished = group.items.some((item) => ['queued', 'in_progress', 'failed'].includes(item.status));
            const completedCount = group.items.filter((item) => item.status === 'completed').length;
            const availableCount = group.items.filter(hasOutput).length;
            const hasPartialOutput = group.items.some((item) => item.output_partial && hasOutput(item));
            const canDelete = canDeleteGroup(group);
            const canDrag = group.status === 'queued' && !actionBusy;
            return (
              <article className={`file-history-card ${draggedGroup === group.id ? 'dragging' : ''}`} key={group.id} draggable={canDrag} onDragStart={() => setDraggedGroup(group.id)} onDragEnd={() => setDraggedGroup(null)} onDragOver={(event) => event.preventDefault()} onDrop={() => reorderGroups(group.id)}>
                <div className="file-history-header">
                  <div className="file-history-main">
                    <input className="history-checkbox" type="checkbox" aria-label={`Select study set for ${group.filename}`} title={canDelete ? `Select ${group.filename}` : 'Deletion is already in progress'} checked={selectedGroupIds.includes(group.id)} disabled={!canDelete || actionBusy} onChange={(event) => { const checked = event.target.checked; setSelectedGroupIds((current) => checked ? [...new Set([...current, group.id])] : current.filter((id) => id !== group.id)); }} />
                    <button type="button" className="accordion-toggle" aria-expanded={isOpen} onClick={() => setExpanded((current) => ({ ...current, [group.id]: !isOpen }))}>
                      {canDrag && <GripVertical size={18} className="drag-handle" />}{isOpen ? <ChevronDown size={18} /> : <ChevronRight size={18} />}<FileText size={22} color="#60a5fa" /><span className="file-history-name">{group.filename}</span><span className={`status-badge status-${group.status}`}>{group.status.replace('_', ' ')}</span>
                    </button>
                  </div>
                  <div className="file-history-summary">
                    <span>{completedCount} / {group.items.length} completed</span>
                    {hasPartialOutput && <span className="partial-output-label">Partial output saved</span>}
                    <span><Clock3 size={13} /> {formatDate(group.created_at)}</span>
                    {availableCount > 0 && <button type="button" className="btn-secondary" disabled={actionBusy} onClick={() => downloadGroup(group)}>
                      {downloadGroupId === group.id ? <RefreshCw size={15} className="spinner" /> : <Download size={15} />} {completedCount === group.items.length && !hasPartialOutput ? 'Download study set' : 'Download available'}
                    </button>}
                    {unfinished && <button type="button" className="btn-secondary btn-danger" disabled={actionBusy} onClick={() => cancelGroup(group)}>{groupAction?.id === group.id && groupAction.type === 'cancel' && <RefreshCw size={15} className="spinner" />} Cancel</button>}
                    <button type="button" className="btn-secondary btn-danger" disabled={actionBusy || !canDelete} onClick={() => deleteGroup(group)}>{groupAction?.id === group.id && groupAction.type === 'delete' ? <RefreshCw size={15} className="spinner" /> : <Trash2 size={15} />} Delete history</button>
                  </div>
                </div>
                {isOpen && <div className="artifact-tree">{group.items.map((artifact) => (
                  <div className="artifact-row" key={artifact.id}>
                    <div className="tree-branch">+--</div>
                    <div className="artifact-content">
                      <div className="artifact-heading"><strong>{artifact.action_title}</strong><span className={`status-badge status-${artifact.status}`}>{artifact.status.replace('_', ' ')}</span><div className="artifact-actions">
                        {hasOutput(artifact) && artifact.download_url && <button type="button" className={`icon-action ${artifact.output_partial ? 'partial' : 'success'}`} disabled={actionBusy} onClick={() => downloadArtifact(artifact)} title={artifact.output_partial ? 'Download partial output (unvalidated)' : 'Download ZIP'} aria-label={artifact.output_partial ? `Download partial output for ${artifact.action_title}` : `Download ${artifact.action_title}`}>{downloadGroupId === `artifact:${artifact.id}` ? <RefreshCw size={15} className="spinner" /> : <Download size={15} />}</button>}
                        {artifact.can_continue && <button type="button" className="icon-action retry" title="Try again" aria-label={`Try ${artifact.action_title} again`} disabled={actionBusy || retryingId !== null} onClick={() => { setRetryArtifact(artifact); setChunkCharacters(String(artifact.metrics?.target_chunk_characters || 2048)); }}><RotateCcw size={15} /></button>}
                        <button type="button" className="icon-action danger" title={artifact.active ? 'Cancel' : 'Delete'} aria-label={`${artifact.active ? 'Cancel' : 'Delete'} ${artifact.action_title}`} disabled={actionBusy || artifact.can_discard === false} onClick={() => discardArtifact(artifact)}><Trash2 size={15} /></button>
                      </div></div>
                      {artifact.output_partial && hasOutput(artifact) && <p className="partial-output-note">Partial output saved. Final validation was not completed.</p>}
                      <div className="artifact-log">{(artifact.log.length ? artifact.log.slice(-4) : [{ at: artifact.updated_at, message: artifact.error || 'No log entries.' }]).map((entry, index) => <div key={`${entry.at}-${index}`}><span>{formatDate(entry.at)}</span> {entry.message}</div>)}<div className="retry-summary">Provider retries: {artifact.retry_count} | Validation retries: {artifact.repair_count} | Runs: {artifact.run_attempts}</div></div>
                    </div>
                  </div>
                ))}</div>}
              </article>
            );
          })}
        </div>
      )}
      {retryArtifact && <div className="modal-backdrop" role="presentation" onMouseDown={() => setRetryArtifact(null)}><div className="retry-modal" role="dialog" aria-modal="true" aria-labelledby="retry-title" onMouseDown={(event) => event.stopPropagation()}>
        <button type="button" className="modal-close" onClick={() => setRetryArtifact(null)} aria-label="Close"><X size={18} /></button><h3 id="retry-title">Try {retryArtifact.action_title} again</h3>
        <label className="form-label" htmlFor="chunk-characters"># of characters of chunk separation</label><div className="character-input"><input id="chunk-characters" type="number" min="256" step="1" className="form-input" value={chunkCharacters} onChange={(event) => setChunkCharacters(event.target.value)} /><span>characters</span></div>
        {!['ollama', 'the_connector'].includes(retryArtifact.connector) && <input type="password" className="form-input" placeholder={retryArtifact.api_key_was_supplied ? 'API key (not stored)' : 'API key override (optional)'} value={retryApiKey} onChange={(event) => setRetryApiKey(event.target.value)} />}
        <div className="modal-actions"><button type="button" className="btn-secondary" onClick={retryConversion}>Okay</button><button type="button" className="btn-secondary" onClick={() => setRetryArtifact(null)}>No</button></div>
      </div></div>}
    </main>
  );
}

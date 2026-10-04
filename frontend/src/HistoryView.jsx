import React, { useCallback, useEffect, useMemo, useState } from 'react';
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

export default function HistoryView() {
  const [conversions, setConversions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState({});
  const [busyId, setBusyId] = useState(null);
  const [downloadGroupId, setDownloadGroupId] = useState(null);
  const [draggedGroup, setDraggedGroup] = useState(null);
  const [retryArtifact, setRetryArtifact] = useState(null);
  const [chunkCharacters, setChunkCharacters] = useState('2048');
  const [retryApiKey, setRetryApiKey] = useState('');

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

  const fetchHistory = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) setLoading(true);
    try {
      const response = await fetch('/api/history');
      if (!response.ok) throw new Error(await responseError(response, 'Could not load file history.'));
      const data = await response.json();
      setConversions(data.conversions || []);
      setError(null);
    } catch (err) {
      setError(err.message || 'Could not load file history.');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchHistory();
    const timer = window.setInterval(() => fetchHistory({ quiet: true }), 2000);
    return () => window.clearInterval(timer);
  }, [fetchHistory]);

  const discardArtifact = async (artifact) => {
    if (!window.confirm(`Delete saved work for ${artifact.action_title}?`)) return;
    setBusyId(artifact.id);
    try {
      const response = await fetch(`/api/history/${artifact.id}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(await responseError(response, 'Could not delete artifact.'));
      await fetchHistory({ quiet: true });
    } catch (err) {
      setError(err.message || 'Could not delete artifact.');
    } finally {
      setBusyId(null);
    }
  };

  const cancelGroup = async (group) => {
    if (!window.confirm(`Cancel all unfinished artifacts for ${group.filename}?`)) return;
    try {
      const response = await fetch(`/api/history/groups/${encodeURIComponent(group.id)}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(await responseError(response, 'Could not cancel file.'));
      await fetchHistory({ quiet: true });
    } catch (err) {
      setError(err.message || 'Could not cancel file.');
    }
  };

  const downloadCompleted = async (group) => {
    setDownloadGroupId(group.id);
    setError(null);
    try {
      const response = await fetch(`/api/history/groups/${encodeURIComponent(group.id)}/download`);
      if (!response.ok) throw new Error(await responseError(response, 'Could not download completed artifacts.'));
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      const stem = group.filename.replace(/\.[^/.]+$/, '') || 'study-set';
      const partial = group.items.some((item) => item.status !== 'completed');
      link.download = `${stem}-study-set${partial ? '-partial' : ''}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      setError(err.message || 'Could not download completed artifacts.');
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
    setBusyId(artifact.id);
    setError(null);
    const formData = new FormData();
    formData.append('chunk_characters', String(size));
    if (retryApiKey.trim()) formData.append('api_key', retryApiKey.trim());
    try {
      const response = await fetch(`/api/history/${artifact.id}/continue`, { method: 'POST', body: formData });
      if (response.status !== 410 && !response.ok) throw new Error(await responseError(response, 'Could not retry artifact.'));
      await fetchHistory({ quiet: true });
    } catch (err) {
      setError(err.message || 'Could not retry artifact.');
      await fetchHistory({ quiet: true });
    } finally {
      setBusyId(null);
      setRetryApiKey('');
    }
  };

  const reorderGroups = async (targetId) => {
    if (!draggedGroup || draggedGroup === targetId) return;
    const ordered = [...groups];
    const from = ordered.findIndex((group) => group.id === draggedGroup);
    const to = ordered.findIndex((group) => group.id === targetId);
    if (from < 0 || to < 0) return;
    const [moved] = ordered.splice(from, 1);
    ordered.splice(to, 0, moved);
    const rank = new Map(ordered.map((group, index) => [group.id, index]));
    setConversions((current) => [...current].sort((a, b) => rank.get(a.source_group_id) - rank.get(b.source_group_id)));
    setDraggedGroup(null);
    try {
      const response = await fetch('/api/history/order', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_ids: ordered.map((group) => group.id) }),
      });
      if (!response.ok) throw new Error(await responseError(response, 'Could not save queue order.'));
      await fetchHistory({ quiet: true });
    } catch (err) {
      setError(err.message || 'Could not save queue order.');
    }
  };

  return (
    <main>
      <div className="history-heading">
        <div><h2 className="page-title"><Archive size={24} /> File History</h2><p className="page-description">Expand a file to manage its artifacts. Drag queued files to change their priority.</p></div>
        <button type="button" className="btn-secondary" onClick={() => fetchHistory()} disabled={loading}><RefreshCw size={16} className={loading ? 'spinner' : ''} /> Refresh</button>
      </div>
      {error && <div className="error-banner"><AlertCircle size={20} /><div>{error}</div></div>}
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
            const canDrag = group.status === 'queued';
            return (
              <article className={`file-history-card ${draggedGroup === group.id ? 'dragging' : ''}`} key={group.id} draggable={canDrag} onDragStart={() => setDraggedGroup(group.id)} onDragOver={(event) => event.preventDefault()} onDrop={() => reorderGroups(group.id)}>
                <div className="file-history-header">
                  <button type="button" className="accordion-toggle" onClick={() => setExpanded((current) => ({ ...current, [group.id]: !isOpen }))}>
                    {canDrag && <GripVertical size={18} className="drag-handle" />}{isOpen ? <ChevronDown size={18} /> : <ChevronRight size={18} />}<FileText size={22} color="#60a5fa" /><span className="file-history-name">{group.filename}</span><span className={`status-badge status-${group.status}`}>{group.status.replace('_', ' ')}</span>
                  </button>
                  <div className="file-history-summary">
                    <span>{completedCount} / {group.items.length} completed</span>
                    <span><Clock3 size={13} /> {formatDate(group.created_at)}</span>
                    {completedCount > 0 && <button type="button" className="btn-secondary" disabled={downloadGroupId !== null} onClick={() => downloadCompleted(group)}>
                      {downloadGroupId === group.id ? <RefreshCw size={15} className="spinner" /> : <Download size={15} />} Download completed
                    </button>}
                    {unfinished && <button type="button" className="btn-secondary btn-danger" onClick={() => cancelGroup(group)}>Cancel</button>}
                  </div>
                </div>
                {isOpen && <div className="artifact-tree">{group.items.map((artifact) => (
                  <div className="artifact-row" key={artifact.id}>
                    <div className="tree-branch">+--</div>
                    <div className="artifact-content">
                      <div className="artifact-heading"><strong>{artifact.action_title}</strong><span className={`status-badge status-${artifact.status}`}>{artifact.status.replace('_', ' ')}</span><div className="artifact-actions">
                        {artifact.status === 'completed' && <a className="icon-action success" href={artifact.download_url} download title="Download ZIP"><Download size={15} /></a>}
                        {artifact.can_continue && <button type="button" className="icon-action retry" title="Try again" disabled={busyId === artifact.id} onClick={() => { setRetryArtifact(artifact); setChunkCharacters(String(artifact.metrics?.target_chunk_characters || 2048)); }}><RotateCcw size={15} /></button>}
                        <button type="button" className="icon-action danger" title={artifact.active ? 'Cancel' : 'Delete'} onClick={() => discardArtifact(artifact)}><Trash2 size={15} /></button>
                      </div></div>
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

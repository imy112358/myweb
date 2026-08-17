(function () {
  'use strict';

  const form = document.getElementById('transcriberForm');
  const tabs = document.querySelectorAll('.mode-tab');
  const linkField = document.getElementById('linkField');
  const fileField = document.getElementById('fileField');
  const sourceInput = document.getElementById('sourceInput');
  const fileInput = document.getElementById('fileInput');
  const cookieSelect = document.getElementById('cookieSelect');
  const preferAudioInput = document.getElementById('preferAudioInput');
  const submitBtn = document.getElementById('submitBtn');
  const copyBtn = document.getElementById('copyBtn');
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const statusHint = document.getElementById('statusHint');
  const resultText = document.getElementById('resultText');
  const resultMeta = document.getElementById('resultMeta');
  const logOutput = document.getElementById('logOutput');
  const jobIdText = document.getElementById('jobIdText');

  let mode = 'link';
  let pollTimer = null;

  function setMode(nextMode) {
    mode = nextMode;
    tabs.forEach(function (tab) {
      tab.classList.toggle('active', tab.dataset.mode === mode);
    });
    linkField.classList.toggle('is-hidden', mode !== 'link');
    fileField.classList.toggle('is-hidden', mode !== 'file');
    cookieSelect.disabled = mode !== 'link';
  }

  function setStatus(status, text, hint) {
    statusDot.dataset.status = status;
    statusText.textContent = text;
    statusHint.textContent = hint || '';
  }

  function formatLogs(logs) {
    if (!logs || !logs.length) return '等待日志...';
    return logs.join('\n');
  }

  async function createJob(event) {
    event.preventDefault();
    clearInterval(pollTimer);

    const data = new FormData();
    data.set('sourceType', mode);
    data.set('cookiesFromBrowser', mode === 'link' ? cookieSelect.value : '');
    data.set('preferAudio', preferAudioInput.checked ? 'true' : 'false');
    data.set('live', 'false');

    if (mode === 'link') {
      const source = sourceInput.value.trim();
      if (!source) {
        setStatus('failed', '缺少链接', '请先粘贴抖音分享链接。');
        return;
      }
      data.set('source', source);
    } else {
      const file = fileInput.files && fileInput.files[0];
      if (!file) {
        setStatus('failed', '缺少文件', '请先选择一个本地视频或音频文件。');
        return;
      }
      data.set('file', file);
    }

    submitBtn.disabled = true;
    copyBtn.disabled = true;
    resultText.value = '';
    resultMeta.textContent = '处理中';
    logOutput.textContent = '任务已提交...';
    jobIdText.textContent = '创建中';
    setStatus('running', '任务提交中', '正在交给本地后端处理。');

    try {
      const response = await fetch('/api/jobs', {
        method: 'POST',
        body: data
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || '创建任务失败');
      }
      jobIdText.textContent = payload.id;
      pollJob(payload.id);
      pollTimer = setInterval(function () {
        pollJob(payload.id);
      }, 1200);
    } catch (error) {
      submitBtn.disabled = false;
      setStatus('failed', '启动失败', error.message);
      logOutput.textContent = error.message;
    }
  }

  async function pollJob(jobId) {
    try {
      const response = await fetch('/api/jobs/' + encodeURIComponent(jobId));
      const job = await response.json();
      if (!response.ok) {
        throw new Error(job.error || '读取任务失败');
      }

      logOutput.textContent = formatLogs(job.logs);
      logOutput.scrollTop = logOutput.scrollHeight;

      if (job.resultText) {
        resultText.value = job.resultText;
        resultMeta.textContent = job.resultText.length + ' 字';
        copyBtn.disabled = false;
      }

      if (job.status === 'queued') {
        setStatus('queued', '排队中', '任务已创建，等待开始。');
      } else if (job.status === 'running') {
        setStatus('running', '正在提取', '正在下载/转码/调用讯飞识别，请稍等。');
      } else if (job.status === 'completed') {
        clearInterval(pollTimer);
        submitBtn.disabled = false;
        setStatus('completed', '提取完成', '文案已生成，可以复制使用。');
      } else if (job.status === 'failed') {
        clearInterval(pollTimer);
        submitBtn.disabled = false;
        setStatus('failed', '提取失败', job.error || '请查看运行日志。');
      }
    } catch (error) {
      clearInterval(pollTimer);
      submitBtn.disabled = false;
      setStatus('failed', '连接失败', error.message);
    }
  }

  async function copyResult() {
    if (!resultText.value.trim()) return;
    await navigator.clipboard.writeText(resultText.value);
    const old = copyBtn.textContent;
    copyBtn.textContent = '已复制';
    setTimeout(function () {
      copyBtn.textContent = old;
    }, 1200);
  }

  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      setMode(tab.dataset.mode);
    });
  });

  if (form) form.addEventListener('submit', createJob);
  if (copyBtn) copyBtn.addEventListener('click', copyResult);
  setMode('link');

  fetch('/api/health')
    .then(function (res) { return res.json(); })
    .then(function (health) {
      if (!health.hasXfyunConfig) {
        setStatus('failed', '缺少讯飞配置', '请确认 /Users/yangmeng/myweb/.xfyun_iat.env 存在。');
      }
    })
    .catch(function () {
      setStatus('failed', '本地服务未启动', '请先运行：python3 tools/transcriber_web_server.py');
    });
})();

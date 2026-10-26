/* Keep figure instances alive across collapse/resize to retain zoom and visibility. */
(() => {
  const colors = ['#3578b5', '#d66a32', '#3d956b', '#ab62ac', '#b58b25',
                  '#378f99', '#c54c61', '#6874ba', '#777c36', '#8d6650'];
  const plots = new Map();
  let queued = false;

  function refresh() {
    for (const [element, state] of plots) {
      if (!element.getClientRects().length || !element.clientWidth) continue;
      if (!state.ready) {
        state.ready = true;
        Plotly.newPlot(element, state.figure.data, state.figure.layout, {
          responsive: true, displaylogo: false, scrollZoom: false
        }).then(() => {
          state.drawn = true;
          if (element.getClientRects().length) Plotly.Plots.resize(element);
        }).catch(() => {
          const message = document.createElement('p');
          message.className = 'notice';
          message.textContent = 'This chart could not be rendered.';
          element.replaceChildren(message);
        });
      } else if (state.drawn) {
        Plotly.Plots.resize(element);
      }
    }
  }

  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      refresh();
    });
  }

  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
  for (const payload of document.querySelectorAll('script[data-figure]')) {
    const figure = JSON.parse(payload.textContent);
    const element = document.getElementById(payload.dataset.figure);
    const legend = element.parentElement.querySelector('.legend');
    figure.layout = {
      ...figure.layout, autosize: true, showlegend: false, title: {text: ''},
      margin: {l: 58, r: 18, t: 18, b: 52, ...figure.layout.margin},
      paper_bgcolor: '#fff', plot_bgcolor: '#fff', uirevision: 'report'
    };
    delete figure.layout.width;
    delete figure.layout.height;
    figure.layout.xaxis = {...figure.layout.xaxis, automargin: true, rangeslider: {visible: false}};
    figure.layout.yaxis = {...figure.layout.yaxis, automargin: true};
    if (figure.data.length && figure.data.every(trace => trace.type === 'box')) {
      // The wrapping legend carries category names without crowding the x axis.
      figure.layout.xaxis.showticklabels = false;
    }
    figure.data.forEach((trace, index) => {
      const palette = figure.layout.colorway || colors;
      const color = trace.line?.color || trace.marker?.color || palette[index % palette.length];
      if (trace.type === 'scatter' || !trace.type) trace.line = {...trace.line, color};
      else if (['bar', 'box', 'violin'].includes(trace.type)) trace.marker = {...trace.marker, color};
      if (trace.showlegend === false) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(trace.visible !== 'legendonly' && trace.visible !== false));
      const swatch = document.createElement('span');
      swatch.className = 'swatch';
      swatch.setAttribute('aria-hidden', 'true');
      swatch.style.setProperty('--color', typeof color === 'string' ? color : colors[index % colors.length]);
      const name = document.createElement('span');
      name.textContent = trace.name || `Series ${index + 1}`;
      button.append(swatch, name);
      button.addEventListener('click', () => {
        const enabled = button.getAttribute('aria-pressed') !== 'true';
        button.setAttribute('aria-pressed', String(enabled));
        trace.visible = enabled ? true : 'legendonly';
        if (plots.get(element).drawn) Plotly.restyle(element, {visible: trace.visible}, [index]);
      });
      legend.append(button);
    });
    plots.set(element, {figure, ready: false, drawn: false});
    observer?.observe(element);
  }
  for (const details of document.querySelectorAll('details')) {
    details.addEventListener('toggle', schedule);
  }
  for (const button of document.querySelectorAll('[data-expand]')) {
    button.addEventListener('click', () => {
      for (const section of document.querySelectorAll('details.section')) {
        section.open = button.dataset.expand === 'true';
      }
      schedule();
    });
  }
  for (const link of document.querySelectorAll('nav a')) {
    link.addEventListener('click', () => {
      const section = document.getElementById(link.hash.slice(1));
      if (section?.tagName === 'DETAILS') section.open = true;
      schedule();
    });
  }
  window.addEventListener('resize', schedule);
  schedule();
})();

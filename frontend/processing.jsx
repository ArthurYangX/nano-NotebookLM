/* global React */

function Processing({ fileName, activeStep }) {
  const steps = [
    { lbl: "Upload", sub: "Streaming bytes to sandbox", tme: "0.4s" },
    { lbl: "OCR & text extraction", sub: "pdfminer · Tesseract fallback", tme: "2.1s" },
    { lbl: "Structural segmentation", sub: "Detecting headings, figures, equations", tme: "1.8s" },
    { lbl: "Embedding & indexing", sub: "1,284 chunks · 768-dim vectors", tme: "3.2s" },
    { lbl: "Cross-source alignment", sub: "Matching against 3 existing sources", tme: "0.9s" },
    { lbl: "Ready", sub: "Notes · mind map · quiz now available", tme: "—" },
  ];
  return (
    <div className="processing">
      <div className="processing-card">
        <div className="eye mono">Ingesting new source</div>
        <h2 className="serif">Preparing your document</h2>
        <div className="fname mono">{fileName}</div>
        <div className="processing-steps">
          {steps.map((s, i) => {
            const cls = i < activeStep ? "pstep done" : i === activeStep ? "pstep active" : "pstep";
            return (
              <div className={cls} key={i}>
                <span className="idx">{i < activeStep ? "✓" : i + 1}</span>
                <span>
                  <span className="lbl">{s.lbl}</span>
                  <div className="sub mono">{s.sub}</div>
                </span>
                <span className="tme">{s.tme}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { Processing });

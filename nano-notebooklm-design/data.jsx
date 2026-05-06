/* global React */
// Sample data for the prototype

const SAMPLE_SOURCES = [
  { id: "s1", type: "pdf", title: "Organic Chemistry — Chapter 7", meta: "142 pp · 4.2 MB", active: true, checked: true, collection: "bio" },
  { id: "s2", type: "ppt", title: "Lecture 12: Stereochemistry", meta: "48 slides · 12 Mar", checked: true, collection: "bio" },
  { id: "s3", type: "pdf", title: "Clayden — Reaction Mechanisms", meta: "§ 8.1–8.4 · 28 pp", checked: true, collection: "bio" },
  { id: "s4", type: "txt", title: "Lab notebook — exp 04-17", meta: "Markdown · 3.4 KB", checked: false, collection: "bio" },
  { id: "s5", type: "img", title: "Whiteboard — TA session", meta: "JPG · 1600×900", checked: false, collection: "bio" },
  { id: "s6", type: "pdf", title: "Problem set 5 — solutions", meta: "12 pp · annotated", checked: false, collection: "hw" },
  { id: "s7", type: "pdf", title: "March 2024 midterm", meta: "Past paper · 8 pp", checked: false, collection: "hw" },
];

const SAMPLE_COLLECTIONS = [
  { id: "bio", name: "Organic Chemistry 301", count: 5, color: "oklch(0.42 0.08 160)" },
  { id: "hw", name: "Homework & exams", count: 2, color: "oklch(0.48 0.12 25)" },
  { id: "ln", name: "Linear Algebra", count: 0, color: "oklch(0.45 0.1 255)" },
];

// Reader body — paragraph objects so we can attach highlights/cites
const READER_DOC = {
  chapter: "Chapter 7 · § 7.3",
  title: "Stereochemistry of Addition Reactions",
  sub: "On the geometric consequences of electrophilic addition to alkenes, with attention to syn- and anti-pathways.",
  body: [
    { kind: "p", text: "The stereochemical outcome of an addition reaction is determined by the relative face selectivity of the two bonds being formed. When both new bonds form on the same face of the alkene, the process is termed syn addition; when they form on opposite faces, the process is anti addition. These two modes, though superficially similar in atom economy, produce configurationally distinct products and therefore require mechanistic justification." },
    { kind: "h2", num: "7.3.1", text: "The bromonium-ion intermediate" },
    { kind: "p", text: "Bromine addition to an alkene proceeds not through a free carbocation but through a ", cites: [{ text: "three-membered cyclic bromonium ion", id: "c1" }, " — a result first established by Roberts and Kimball (1937)."] },
    { kind: "p", text: "The bromonium ion is opened by nucleophilic attack on the face opposite to the bromine bridge. This Markovnikov-consistent but anti-selective opening explains the nearly exclusive production of (R,R) and (S,S) dibromides from trans-alkenes, a classical signature of anti addition." },
    { kind: "figure", num: "7.3", caption: "The bromonium-ion intermediate and its two diastereotopic opening pathways.", body: "Bromonium mechanism diagram" },
    { kind: "h2", num: "7.3.2", text: "Hydroboration–oxidation and the syn pathway" },
    { kind: "p", text: "In contrast, hydroboration proceeds through a ", cites: [{ text: "four-centred concerted transition state", id: "c2" }, " in which the B–H bond breaks on the same face as the new C–H and C–B bonds form. The resulting alkylborane, upon oxidation with alkaline hydrogen peroxide, yields an alcohol whose stereochemistry is strictly syn."] },
    { kind: "p", text: "This distinction — anti for electrophilic halogenation, syn for hydroboration — is the canonical organising principle of Chapter 7 and should be understood not as two special cases, but as two points on a continuum of transition-state geometries." },
  ]
};

const NOTES_DATA = {
  title: "Stereochemistry of Addition Reactions",
  generated: "Generated from 3 sources · 16 min read",
  outline: [
    {
      h: "The two addition modes",
      roman: "I.",
      p: "Every alkene addition can be classified by face-selectivity. Syn addition delivers both new substituents to the same π-face; anti addition delivers them to opposite faces. The outcome determines the diastereomer obtained.",
      subs: [
        { b: "Syn addition", t: "— both bonds on the same face (e.g. hydroboration, OsO₄ dihydroxylation, catalytic hydrogenation)." },
        { b: "Anti addition", t: "— bonds on opposite faces (e.g. Br₂, HBr via bromonium, epoxide opening)." },
      ]
    },
    {
      h: "Bromonium-ion mechanism",
      roman: "II.",
      p: "Electrophilic halogens form a three-membered cyclic halonium ion. Nucleophilic attack on the back face produces the anti-dihalide.",
      subs: [
        { b: "Evidence", t: "Trans-alkenes yield (R,R)/(S,S) pairs exclusively — no syn product detectable." },
        { b: "Kinetics", t: "Rate law is first-order in alkene and first-order in halogen; no carbocation rearrangements observed." },
      ],
      callout: "A free carbocation would rearrange and scramble stereochemistry — the bromonium bridge explicitly prevents this."
    },
    {
      h: "Hydroboration as the canonical syn case",
      roman: "III.",
      p: "The B–H bond adds across the π-bond through a four-centred, cyclic transition state. All bond-making and bond-breaking occurs in a single step, forcing both new bonds onto the same face.",
      subs: [
        { b: "Regiochemistry", t: "Anti-Markovnikov — boron attaches to the less-substituted carbon due to steric and electronic factors." },
        { b: "Oxidation step", t: "H₂O₂/OH⁻ replaces B with OH while retaining configuration." },
      ]
    }
  ]
};

const QUIZ_DATA = {
  title: "Stereochemistry — Midterm Practice",
  meta: [
    { k: "Topic", v: "§ 7.3" },
    { k: "Questions", v: "6" },
    { k: "Difficulty", v: "Moderate" },
    { k: "Est. time", v: "18 min" }
  ],
  questions: [
    {
      type: "multiple choice",
      pts: 2,
      prompt: "Which of the following reactions proceeds through a bromonium-ion intermediate and gives exclusively anti-addition?",
      options: [
        { l: "A", t: "Hydroboration–oxidation of cyclohexene with BH₃·THF followed by H₂O₂/NaOH." },
        { l: "B", t: "Reaction of (E)-but-2-ene with Br₂ in CH₂Cl₂ at 0 °C." },
        { l: "C", t: "Catalytic hydrogenation of (Z)-stilbene with Pd/C and H₂ at 1 atm." },
        { l: "D", t: "Dihydroxylation of styrene with OsO₄ and NMO." }
      ],
      correct: "B",
      explain: "Bromine addition to an alkene proceeds through a cyclic bromonium ion; nucleophilic attack on the opposite face gives exclusively anti-dibromide. The other three are syn pathways."
    },
    {
      type: "short answer",
      pts: 4,
      prompt: "Briefly explain why the hydroboration of 1-methylcyclopentene gives trans-2-methylcyclopentan-1-ol as the major product. Reference both regio- and stereochemistry."
    },
    {
      type: "multiple choice",
      pts: 2,
      prompt: "The four-centred transition state of hydroboration forces all new bonds onto the same face of the alkene. This is best described as:",
      options: [
        { l: "A", t: "A stepwise ionic mechanism." },
        { l: "B", t: "A concerted syn-selective pericyclic addition." },
        { l: "C", t: "A radical chain process with trans selectivity." },
        { l: "D", t: "An SN2-type displacement at the alkene carbon." }
      ]
    }
  ]
};

// Mind map tree
const MINDMAP = {
  id: "root",
  label: "Stereochemistry of addition",
  children: [
    {
      id: "anti", label: "Anti addition",
      children: [
        { id: "br2", label: "Br₂ / Cl₂", children: [
          { id: "bromonium", label: "Bromonium ion" },
          { id: "backattack", label: "Back-face attack" }
        ]},
        { id: "hx", label: "H–X via halonium" },
        { id: "epoxide", label: "Epoxide opening (acidic)" }
      ]
    },
    {
      id: "syn", label: "Syn addition",
      children: [
        { id: "hb", label: "Hydroboration–oxidation", children: [
          { id: "ts4", label: "4-centre transition state" },
          { id: "antimark", label: "Anti-Markovnikov" }
        ]},
        { id: "oso4", label: "OsO₄ dihydroxylation" },
        { id: "h2pd", label: "H₂ / Pd catalytic" }
      ]
    },
    {
      id: "evidence", label: "Evidence",
      children: [
        { id: "stereo", label: "Stereospecific products" },
        { id: "kin", label: "Kinetics · rate laws" },
        { id: "isotope", label: "Isotope-labelling studies" }
      ]
    }
  ]
};

Object.assign(window, { SAMPLE_SOURCES, SAMPLE_COLLECTIONS, READER_DOC, NOTES_DATA, QUIZ_DATA, MINDMAP });

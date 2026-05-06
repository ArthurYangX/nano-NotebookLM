%% IEEEtranCN.cls - wrapper of IEEEtran.cls with Chinese support
\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{IEEEtranCN}[2026/01/24 IEEEtran wrapper with Chinese support]

% Pass all options to IEEEtran
\DeclareOption*{\PassOptionsToClass{\CurrentOption}{IEEEtran}}
\ProcessOptions\relax

% Load the original IEEE class
\LoadClass{IEEEtran}

% Chinese support (XeLaTeX/LuaLaTeX)
\RequirePackage{iftex}
\ifPDFTeX
  % pdfLaTeX doesn't handle Chinese well here; force user to switch compiler
  \ClassError{IEEEtranCN}{Please use XeLaTeX or LuaLaTeX in Overleaf (Menu -> Compiler)}{}
\else
  \RequirePackage{fontspec}
  \RequirePackage{xeCJK}

  % macOS Chinese fonts
  \setCJKmainfont{Songti SC}
  \setCJKsansfont{Heiti SC}
  \setCJKmonofont{STFangsong}

  % Optional: better line breaking for Chinese
  \XeTeXlinebreaklocale "zh"
  \XeTeXlinebreakskip = 0pt plus 1pt

  \xeCJKsetup{CJKmath=true}
\fi



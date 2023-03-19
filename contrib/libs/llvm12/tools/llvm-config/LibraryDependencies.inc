
  struct AvailableComponent {
    /// The name of the component.
    const char *Name;

    /// The name of the library for this component (or NULL).
    const char *Library;

    /// Whether the component is installed.
    bool IsInstalled;

    /// The list of libraries required when linking this component.
    const char *RequiredLibraries[110];
  } AvailableComponents[110] = {
  { "aarch64", nullptr, true, {"aarch64codegen", "aarch64asmparser", "aarch64disassembler", "aarch64desc", "aarch64info", "aarch64utils"} },
{ "aarch64asmparser", "LLVMAArch64AsmParser", true, {"aarch64desc", "aarch64info", "aarch64utils", "mc", "mcparser", "support"} },
{ "aarch64codegen", "LLVMAArch64CodeGen", true, {"aarch64desc", "aarch64info", "aarch64utils", "analysis", "asmprinter", "codegen", "core", "mc", "scalaropts", "selectiondag", "support", "target", "transformutils", "globalisel", "cfguard"} },
{ "aarch64desc", "LLVMAArch64Desc", true, {"aarch64info", "aarch64utils", "mc", "binaryformat", "support"} },
{ "aarch64disassembler", "LLVMAArch64Disassembler", true, {"aarch64desc", "aarch64info", "aarch64utils", "mc", "mcdisassembler", "support"} },
{ "aarch64info", "LLVMAArch64Info", true, {"support"} },
{ "aarch64utils", "LLVMAArch64Utils", true, {"support"} },
{ "aggressiveinstcombine", "LLVMAggressiveInstCombine", true, {"analysis", "core", "support", "transformutils"} },
{ "all", nullptr, true, {"demangle", "support", "tablegen", "core", "fuzzmutate", "filecheck", "interfacestub", "irreader", "codegen", "selectiondag", "asmprinter", "mirparser", "globalisel", "binaryformat", "bitreader", "bitwriter", "bitstreamreader", "dwarflinker", "extensions", "frontendopenacc", "frontendopenmp", "transformutils", "instrumentation", "aggressiveinstcombine", "instcombine", "scalaropts", "ipo", "vectorize", "hellonew", "objcarcopts", "coroutines", "cfguard", "linker", "analysis", "lto", "mc", "mcparser", "mcdisassembler", "mca", "object", "objectyaml", "option", "remarks", "debuginfodwarf", "debuginfogsym", "debuginfomsf", "debuginfocodeview", "debuginfopdb", "symbolize", "executionengine", "interpreter", "jitlink", "mcjit", "orcjit", "orcshared", "orctargetprocess", "runtimedyld", "perfjitevents", "target", "aarch64codegen", "aarch64asmparser", "aarch64disassembler", "aarch64desc", "aarch64info", "aarch64utils", "armcodegen", "armasmparser", "armdisassembler", "armdesc", "arminfo", "armutils", "bpfcodegen", "bpfasmparser", "bpfdisassembler", "bpfdesc", "bpfinfo", "powerpccodegen", "powerpcasmparser", "powerpcdisassembler", "powerpcdesc", "powerpcinfo", "x86codegen", "x86asmparser", "x86disassembler", "x86desc", "x86info", "nvptxcodegen", "nvptxdesc", "nvptxinfo", "asmparser", "lineeditor", "profiledata", "coverage", "passes", "textapi", "dlltooldriver", "libdriver", "xray", "windowsmanifest", "all-targets", "engine", "native", "nativecodegen", "aarch64", "arm", "bpf", "powerpc", "x86", "nvptx"} },
{ "all-targets", nullptr, true, {"aarch64", "arm", "bpf", "powerpc", "x86", "nvptx"} },
{ "analysis", "LLVMAnalysis", true, {"binaryformat", "core", "object", "profiledata", "support"} },
{ "arm", nullptr, true, {"armcodegen", "armasmparser", "armdisassembler", "armdesc", "arminfo", "armutils"} },
{ "armasmparser", "LLVMARMAsmParser", true, {"armdesc", "arminfo", "mc", "mcparser", "support", "armutils"} },
{ "armcodegen", "LLVMARMCodeGen", true, {"armdesc", "arminfo", "analysis", "asmprinter", "codegen", "core", "mc", "scalaropts", "selectiondag", "support", "target", "globalisel", "armutils", "transformutils", "cfguard"} },
{ "armdesc", "LLVMARMDesc", true, {"arminfo", "armutils", "mc", "mcdisassembler", "support", "binaryformat"} },
{ "armdisassembler", "LLVMARMDisassembler", true, {"armdesc", "arminfo", "mcdisassembler", "support", "armutils"} },
{ "arminfo", "LLVMARMInfo", true, {"support"} },
{ "armutils", "LLVMARMUtils", true, {"support"} },
{ "asmparser", "LLVMAsmParser", true, {"binaryformat", "core", "support"} },
{ "asmprinter", "LLVMAsmPrinter", true, {"analysis", "binaryformat", "codegen", "core", "debuginfocodeview", "debuginfodwarf", "debuginfomsf", "mc", "mcparser", "remarks", "support", "target"} },
{ "binaryformat", "LLVMBinaryFormat", true, {"support"} },
{ "bitreader", "LLVMBitReader", true, {"bitstreamreader", "core", "support"} },
{ "bitstreamreader", "LLVMBitstreamReader", true, {"support"} },
{ "bitwriter", "LLVMBitWriter", true, {"analysis", "core", "mc", "object", "support"} },
{ "bpf", nullptr, true, {"bpfcodegen", "bpfasmparser", "bpfdisassembler", "bpfdesc", "bpfinfo"} },
{ "bpfasmparser", "LLVMBPFAsmParser", true, {"mc", "mcparser", "bpfdesc", "bpfinfo", "support"} },
{ "bpfcodegen", "LLVMBPFCodeGen", true, {"analysis", "asmprinter", "codegen", "core", "mc", "bpfdesc", "bpfinfo", "ipo", "scalaropts", "selectiondag", "support", "target", "transformutils"} },
{ "bpfdesc", "LLVMBPFDesc", true, {"mc", "bpfinfo", "support"} },
{ "bpfdisassembler", "LLVMBPFDisassembler", true, {"mcdisassembler", "bpfinfo", "support"} },
{ "bpfinfo", "LLVMBPFInfo", true, {"support"} },
{ "cfguard", "LLVMCFGuard", true, {"core", "support"} },
{ "codegen", "LLVMCodeGen", true, {"analysis", "bitreader", "bitwriter", "core", "mc", "profiledata", "scalaropts", "support", "target", "transformutils"} },
{ "core", "LLVMCore", true, {"binaryformat", "remarks", "support"} },
{ "coroutines", "LLVMCoroutines", true, {"analysis", "core", "ipo", "scalaropts", "support", "transformutils"} },
{ "coverage", "LLVMCoverage", true, {"core", "object", "profiledata", "support"} },
{ "debuginfocodeview", "LLVMDebugInfoCodeView", true, {"support", "debuginfomsf"} },
{ "debuginfodwarf", "LLVMDebugInfoDWARF", true, {"binaryformat", "object", "mc", "support"} },
{ "debuginfogsym", "LLVMDebugInfoGSYM", true, {"mc", "object", "support", "debuginfodwarf"} },
{ "debuginfomsf", "LLVMDebugInfoMSF", true, {"support"} },
{ "debuginfopdb", "LLVMDebugInfoPDB", true, {"binaryformat", "object", "support", "debuginfocodeview", "debuginfomsf"} },
{ "demangle", "LLVMDemangle", true, {} },
{ "dlltooldriver", "LLVMDlltoolDriver", true, {"object", "option", "support"} },
{ "dwarflinker", "LLVMDWARFLinker", true, {"binaryformat", "debuginfodwarf", "asmprinter", "codegen", "mc", "object", "support"} },
{ "engine", nullptr, true, {"interpreter"} },
{ "executionengine", "LLVMExecutionEngine", true, {"core", "mc", "object", "runtimedyld", "support", "target"} },
{ "extensions", "LLVMExtensions", true, {"support"} },
{ "filecheck", "LLVMFileCheck", true, {} },
{ "frontendopenacc", "LLVMFrontendOpenACC", true, {} },
{ "frontendopenmp", "LLVMFrontendOpenMP", true, {"core", "support", "transformutils"} },
{ "fuzzmutate", "LLVMFuzzMutate", true, {"analysis", "bitreader", "bitwriter", "core", "scalaropts", "support", "target"} },
{ "globalisel", "LLVMGlobalISel", true, {"analysis", "codegen", "core", "mc", "selectiondag", "support", "target", "transformutils"} },
{ "hellonew", "LLVMHelloNew", true, {"core", "support"} },
{ "instcombine", "LLVMInstCombine", true, {"analysis", "core", "support", "transformutils"} },
{ "instrumentation", "LLVMInstrumentation", true, {"analysis", "core", "mc", "support", "transformutils", "profiledata"} },
{ "interfacestub", "LLVMInterfaceStub", true, {"mc", "object", "support"} },
{ "interpreter", "LLVMInterpreter", true, {"codegen", "core", "executionengine", "support"} },
{ "ipo", "LLVMipo", true, {"aggressiveinstcombine", "analysis", "bitreader", "bitwriter", "core", "frontendopenmp", "instcombine", "irreader", "linker", "object", "profiledata", "scalaropts", "support", "transformutils", "vectorize", "instrumentation"} },
{ "irreader", "LLVMIRReader", true, {"asmparser", "bitreader", "core", "support"} },
{ "jitlink", "LLVMJITLink", true, {"binaryformat", "object", "orctargetprocess", "support"} },
{ "libdriver", "LLVMLibDriver", true, {"binaryformat", "bitreader", "object", "option", "support", "binaryformat", "bitreader", "object", "option", "support"} },
{ "lineeditor", "LLVMLineEditor", true, {"support"} },
{ "linker", "LLVMLinker", true, {"core", "object", "support", "transformutils"} },
{ "lto", "LLVMLTO", true, {"aggressiveinstcombine", "analysis", "binaryformat", "bitreader", "bitwriter", "codegen", "core", "extensions", "ipo", "instcombine", "linker", "mc", "objcarcopts", "object", "passes", "remarks", "scalaropts", "support", "target", "transformutils"} },
{ "mc", "LLVMMC", true, {"support", "binaryformat", "debuginfocodeview"} },
{ "mca", "LLVMMCA", true, {"mc", "support"} },
{ "mcdisassembler", "LLVMMCDisassembler", true, {"mc", "support"} },
{ "mcjit", "LLVMMCJIT", true, {"core", "executionengine", "object", "runtimedyld", "support", "target"} },
{ "mcparser", "LLVMMCParser", true, {"mc", "support"} },
{ "mirparser", "LLVMMIRParser", true, {"asmparser", "binaryformat", "codegen", "core", "mc", "support", "target"} },
{ "native", nullptr, true, {"x86"} },
{ "nativecodegen", nullptr, true, {"x86codegen"} },
{ "nvptx", nullptr, true, {"nvptxcodegen", "nvptxdesc", "nvptxinfo"} },
{ "nvptxcodegen", "LLVMNVPTXCodeGen", true, {"analysis", "asmprinter", "codegen", "core", "ipo", "mc", "nvptxdesc", "nvptxinfo", "scalaropts", "selectiondag", "support", "target", "transformutils", "vectorize"} },
{ "nvptxdesc", "LLVMNVPTXDesc", true, {"mc", "nvptxinfo", "support"} },
{ "nvptxinfo", "LLVMNVPTXInfo", true, {"support"} },
{ "objcarcopts", "LLVMObjCARCOpts", true, {"analysis", "core", "support", "transformutils"} },
{ "object", "LLVMObject", true, {"bitreader", "core", "mc", "binaryformat", "mcparser", "support", "textapi"} },
{ "objectyaml", "LLVMObjectYAML", true, {"binaryformat", "object", "support", "debuginfocodeview", "mc"} },
{ "option", "LLVMOption", true, {"support"} },
{ "orcjit", "LLVMOrcJIT", true, {"core", "executionengine", "jitlink", "object", "orcshared", "orctargetprocess", "mc", "passes", "runtimedyld", "support", "target", "transformutils"} },
{ "orcshared", "LLVMOrcShared", true, {"support"} },
{ "orctargetprocess", "LLVMOrcTargetProcess", true, {"orcshared", "support"} },
{ "passes", "LLVMPasses", true, {"aggressiveinstcombine", "analysis", "core", "coroutines", "hellonew", "ipo", "instcombine", "objcarcopts", "scalaropts", "support", "target", "transformutils", "vectorize", "instrumentation"} },
{ "perfjitevents", "LLVMPerfJITEvents", true, {"codegen", "core", "debuginfodwarf", "executionengine", "object", "support"} },
{ "powerpc", nullptr, true, {"powerpccodegen", "powerpcasmparser", "powerpcdisassembler", "powerpcdesc", "powerpcinfo"} },
{ "powerpcasmparser", "LLVMPowerPCAsmParser", true, {"mc", "mcparser", "powerpcdesc", "powerpcinfo", "support"} },
{ "powerpccodegen", "LLVMPowerPCCodeGen", true, {"analysis", "asmprinter", "binaryformat", "codegen", "core", "mc", "powerpcdesc", "powerpcinfo", "scalaropts", "selectiondag", "support", "target", "transformutils", "globalisel"} },
{ "powerpcdesc", "LLVMPowerPCDesc", true, {"mc", "powerpcinfo", "support", "binaryformat"} },
{ "powerpcdisassembler", "LLVMPowerPCDisassembler", true, {"mcdisassembler", "powerpcinfo", "support"} },
{ "powerpcinfo", "LLVMPowerPCInfo", true, {"support"} },
{ "profiledata", "LLVMProfileData", true, {"core", "support", "demangle"} },
{ "remarks", "LLVMRemarks", true, {"bitstreamreader", "support"} },
{ "runtimedyld", "LLVMRuntimeDyld", true, {"core", "mc", "object", "support"} },
{ "scalaropts", "LLVMScalarOpts", true, {"aggressiveinstcombine", "analysis", "core", "instcombine", "support", "transformutils"} },
{ "selectiondag", "LLVMSelectionDAG", true, {"analysis", "codegen", "core", "mc", "support", "target", "transformutils"} },
{ "support", "LLVMSupport", true, {"demangle"} },
{ "symbolize", "LLVMSymbolize", true, {"debuginfodwarf", "debuginfopdb", "object", "support", "demangle"} },
{ "tablegen", "LLVMTableGen", true, {"support"} },
{ "target", "LLVMTarget", true, {"analysis", "core", "mc", "support"} },
{ "textapi", "LLVMTextAPI", true, {"support", "binaryformat"} },
{ "transformutils", "LLVMTransformUtils", true, {"analysis", "core", "support"} },
{ "vectorize", "LLVMVectorize", true, {"analysis", "core", "support", "transformutils"} },
{ "windowsmanifest", "LLVMWindowsManifest", true, {"support"} },
{ "x86", nullptr, true, {"x86codegen", "x86asmparser", "x86disassembler", "x86desc", "x86info"} },
{ "x86asmparser", "LLVMX86AsmParser", true, {"mc", "mcparser", "support", "x86desc", "x86info"} },
{ "x86codegen", "LLVMX86CodeGen", true, {"analysis", "asmprinter", "codegen", "core", "mc", "selectiondag", "support", "target", "x86desc", "x86info", "globalisel", "profiledata", "cfguard"} },
{ "x86desc", "LLVMX86Desc", true, {"mc", "mcdisassembler", "support", "x86info", "binaryformat"} },
{ "x86disassembler", "LLVMX86Disassembler", true, {"mcdisassembler", "support", "x86info"} },
{ "x86info", "LLVMX86Info", true, {"support"} },
{ "xray", "LLVMXRay", true, {"support", "object"} },
};
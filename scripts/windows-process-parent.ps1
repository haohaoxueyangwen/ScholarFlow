# Query parent identity without WMI elevation or persistent system changes.
if (-not ('ScholarFlow.NativeProcessIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace ScholarFlow {
  public static class NativeProcessIdentity {
    [StructLayout(LayoutKind.Sequential)]
    private struct BasicInfo {
      public IntPtr Reserved1;
      public IntPtr PebBaseAddress;
      public IntPtr Reserved2a;
      public IntPtr Reserved2b;
      public IntPtr UniqueProcessId;
      public IntPtr InheritedFromUniqueProcessId;
    }
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern IntPtr OpenProcess(uint access, bool inheritHandle, int processId);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(IntPtr process, int informationClass, ref BasicInfo info, int length, out int returned);
    public static int ParentId(int pid) {
      IntPtr handle = OpenProcess(0x1000, false, pid);
      if (handle == IntPtr.Zero) return 0;
      try {
        BasicInfo info = new BasicInfo();
        int returned;
        int status = NtQueryInformationProcess(handle, 0, ref info, Marshal.SizeOf(typeof(BasicInfo)), out returned);
        return status == 0 ? unchecked((int)info.InheritedFromUniqueProcessId.ToInt64()) : 0;
      } finally { CloseHandle(handle); }
    }
  }
}
'@
}

function Get-ScholarFlowParentProcessId {
    param([int] $ProcessId)
    return [ScholarFlow.NativeProcessIdentity]::ParentId($ProcessId)
}

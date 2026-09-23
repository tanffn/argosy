<# Capture the user's authorized Alpha page in normal Chrome. No cookie export,
   credential reading, keystroke injection, extension, or remote-debugging port. #>
param(
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][string]$GoogleEmail
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class ArgosyAlphaNative {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern IntPtr OpenInputDesktop(uint f, bool i, uint a);
  [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SwitchDesktop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint flags);
  [DllImport("user32.dll", EntryPoint="SendMessageW")] public static extern IntPtr NotifyMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, string v);
  [DllImport("user32.dll", EntryPoint="SendMessageW", CharSet=CharSet.Unicode)] public static extern IntPtr ReadMessage(IntPtr h, uint m, IntPtr w, StringBuilder v);
  public static bool Unlocked() {
    var h=OpenInputDesktop(0,false,0x100); if(h==IntPtr.Zero) return false;
    try { return SwitchDesktop(h); } finally { CloseDesktop(h); }
  }
  public static bool Save(IntPtr root, string filename) {
    IntPtr edit=IntPtr.Zero,save=IntPtr.Zero; bool complete=false;
    EnumChildWindows(root,(h,p)=>{var cls=new StringBuilder(128);GetClassName(h,cls,128);
      if(cls.ToString()=="Edit" && GetDlgCtrlID(h)==1001) edit=h;
      if(cls.ToString()=="Button" && GetDlgCtrlID(h)==1) save=h;
      if(cls.ToString()=="ComboBox") {
        int count=(int)SendMessage(h,0x146,IntPtr.Zero,null);
        for(int i=0;i<Math.Min(count,30);i++) {
          int length=(int)SendMessage(h,0x149,(IntPtr)i,null); if(length<0 || length>=4000) continue;
          var label=new StringBuilder(4096);ReadMessage(h,0x148,(IntPtr)i,label);
          if(label.ToString().IndexOf("Webpage, Complete",StringComparison.OrdinalIgnoreCase)>=0) {
            SendMessage(h,0x14E,(IntPtr)i,null);
            NotifyMessage(GetParent(h),0x111,(IntPtr)(GetDlgCtrlID(h)|(1<<16)),h);
            complete=(int)SendMessage(h,0x147,IntPtr.Zero,null)==i;
          }
        }
      }
      return true;},IntPtr.Zero);
    if(edit==IntPtr.Zero || save==IntPtr.Zero || !complete) return false;
    SendMessage(edit,0xC,IntPtr.Zero,filename);
    var actual=new StringBuilder(4096);ReadMessage(edit,0xD,(IntPtr)4096,actual);
    if(actual.ToString()!=filename) return false;
    SendMessage(save,0xF5,IntPtr.Zero,null);return true;
  }
}
'@
$ArgosyDesktop = [System.Windows.Automation.AutomationElement]::RootElement
$ArgosyScope = [System.Windows.Automation.TreeScope]::Descendants
# The app redirects logged-out sessions to its separate REINVEST SSO origin.
# Exact hosts only; never accept arbitrary subdomains or OAuth callback URLs.
$ArgosySiteHosts = @('app.meetkevin.com', 'sso.meet-kevin.com')
function Get-Named($Root, [string]$Name, [switch]$Invokable) {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, $Name)
    if (-not $Invokable) { return $Root.FindFirst($ArgosyScope, $condition) }
    # SSO exposes a list item and its link with the same accessible name.
    # Select the action itself, not the first matching presentation container.
    $matches = @($Root.FindAll($ArgosyScope, $condition) | Where-Object {
        $pattern = $null
        $_.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)
    })
    if ($matches.Count -gt 1) { throw 'Ambiguous named browser action; refusing interaction' }
    if ($matches.Count -eq 1) { return $matches[0] }
}
function Get-Address($Root) {
    $edit = $Root.FindFirst($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'view_1012')))
    if ($null -eq $edit) { return '' }
    $value = $edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
    if ($value -notmatch '^https?://') { $value = 'https://' + $value }
    return $value
}
function Invoke-Control($Control) {
    if ($null -eq $Control) { throw 'Expected browser control is absent; page layout may have changed' }
    Assert-OwnedWindow
    $current = [Uri](Get-Address $ArgosyWindow)
    if ($current.Scheme -ne 'https' -or $current.Port -ne 443 -or
        ($current.Host -notin $ArgosySiteHosts -and -not ($ArgosyGoogleClicked -and $current.Host -eq 'accounts.google.com'))) {
        throw 'Browser origin changed before interaction'
    }
    $Control.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}
function Assert-OwnedWindow {
    $foreground = [ArgosyAlphaNative]::GetForegroundWindow()
    $owner = [ArgosyAlphaNative]::GetAncestor($foreground, 3)
    if ($foreground -ne [IntPtr]$ArgosyWindow.Current.NativeWindowHandle -and $owner -ne [IntPtr]$ArgosyWindow.Current.NativeWindowHandle) {
        throw 'Another window took focus; capture stopped without interacting with it'
    }
}
function Assert-Alpha {
    Assert-OwnedWindow
    if ((Get-Address $ArgosyWindow).TrimEnd('/') -ne 'https://app.meetkevin.com/data/alpha') {
        throw 'Alpha URL changed before save interaction'
    }
}
function Get-ChromeWindows {
    $ids = @(Get-Process chrome -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    @($ArgosyDesktop.FindAll([System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.Condition]::TrueCondition) | Where-Object { $ids -contains $_.Current.ProcessId })
}
if (-not [ArgosyAlphaNative]::Unlocked()) { throw 'Desktop is locked; browser was not opened' }
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $OutputPath) { throw 'Capture target already exists; refusing overwrite' }
if (-not (Test-Path -LiteralPath (Split-Path -Parent $OutputPath))) { throw 'Capture output directory is missing' }
$ArgosyBefore = @(Get-ChromeWindows | ForEach-Object { $_.Current.NativeWindowHandle })
$ArgosyChrome = @('C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe') | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $ArgosyChrome) { throw 'Normal Google Chrome is not installed' }
# A visible browser is intentional; the caller's PowerShell process is hidden.
Start-Process -FilePath $ArgosyChrome -ArgumentList '--new-window','https://app.meetkevin.com/data/alpha' | Out-Null
$ArgosyDeadline = [DateTime]::UtcNow.AddSeconds(180)
$ArgosyWindow = $null
$ArgosyGoogleClicked = $false
$ArgosyAccountClicked = $false
$ArgosyReady = $false
while ([DateTime]::UtcNow -lt $ArgosyDeadline) {
    Start-Sleep -Milliseconds 600
    if (-not [ArgosyAlphaNative]::Unlocked()) { throw 'Desktop locked during capture; attempt consumed, no automatic reopening today' }
    if ($null -eq $ArgosyWindow) {
        $ArgosyWindow = Get-ChromeWindows | Where-Object {
            $ArgosyBefore -notcontains $_.Current.NativeWindowHandle -and
            (Get-Address $_) -match '^https://(app\.meetkevin\.com|sso\.meet-kevin\.com)/'
        } | Select-Object -First 1
        if ($null -eq $ArgosyWindow) { continue }
    }
    $ArgosyAddress = [Uri](Get-Address $ArgosyWindow)
    if ($ArgosyAddress.Scheme -ne 'https' -or $ArgosyAddress.Port -ne 443) { throw 'Unexpected insecure browser origin' }
    if ($ArgosyAddress.Host -eq 'accounts.google.com' -and $ArgosyGoogleClicked) {
        if (-not $ArgosyAccountClicked) {
            $choices = @($ArgosyWindow.FindAll($ArgosyScope,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::Hyperlink))) | Where-Object {
                        $_.Current.Name -match ('(?<!\S)' + [regex]::Escape($GoogleEmail) + '(?!\S)') })
            if ($choices.Count -eq 1) { Invoke-Control $choices[0]; $ArgosyAccountClicked = $true }
        }
        continue # Password/MFA/CAPTCHA is for the user; never inspect or bypass.
    }
    if ($ArgosyAddress.Host -notin $ArgosySiteHosts) {
        # Host only: OAuth URLs can contain authorization codes and state.
        throw "Browser left the authorized login/site origins (host=$($ArgosyAddress.Host); google_started=$ArgosyGoogleClicked)"
    }
    $ArgosyLogin = Get-Named $ArgosyWindow 'Sign in with Google' -Invokable
    if ($null -ne $ArgosyLogin -and -not $ArgosyGoogleClicked) {
        Invoke-Control $ArgosyLogin; $ArgosyGoogleClicked = $true; continue
    }
    if ($ArgosyAddress.AbsolutePath.TrimEnd('/') -eq '/data/alpha') {
        $doc = $ArgosyWindow.FindFirst($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Document)))
        if ($null -ne $doc) {
            $headings = @($doc.FindAll($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Text))) | Where-Object { $_.Current.Name -match '^\d+-\d+-\d{4} Alpha Report:' })
            if ($headings.Count -gt 0) { $ArgosyReady = $true; break }
        }
    } else {
        $alpha = Get-Named $ArgosyWindow 'ALPHA' -Invokable
        if ($null -ne $alpha) { Invoke-Control $alpha; continue }
        $data = Get-Named $ArgosyWindow 'DATA' -Invokable
        if ($null -ne $data) { Invoke-Control $data; continue }
    }
}
if (-not $ArgosyReady) { throw '[LOGIN_REQUIRED] Login or page load needs attention in the open Chrome window; no capture imported' }
if ((Get-Address $ArgosyWindow).TrimEnd('/') -ne 'https://app.meetkevin.com/data/alpha') { throw 'Alpha URL changed before save' }
$ArgosyMenu = $ArgosyWindow.FindFirst($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'view_1007')))
Assert-Alpha
$ArgosyMenu.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Start-Sleep -Milliseconds 300
$ArgosyMenus = $ArgosyDesktop.FindAll($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::MenuItem)))
$ArgosyShare = $ArgosyMenus | Where-Object { $_.Current.ProcessId -eq $ArgosyWindow.Current.ProcessId -and $_.Current.Name -eq 'Cast, save, and share' } | Select-Object -First 1
Assert-Alpha
$ArgosyShare.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Start-Sleep -Milliseconds 300
$ArgosySave = $ArgosyDesktop.FindAll($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::MenuItem))) |
    Where-Object { $_.Current.ProcessId -eq $ArgosyWindow.Current.ProcessId -and $_.Current.Name -match '^Save page as' } | Select-Object -First 1
Assert-Alpha
Invoke-Control $ArgosySave
$ArgosySaveDeadline = [DateTime]::UtcNow.AddSeconds(12)
$ArgosyDialog = $null
while ($null -eq $ArgosyDialog -and [DateTime]::UtcNow -lt $ArgosySaveDeadline) {
    Start-Sleep -Milliseconds 300
    $ArgosyDialog = Get-Named $ArgosyWindow 'Save As'
}
if ($null -eq $ArgosyDialog) { throw 'Chrome Save As dialog did not appear' }
Assert-Alpha
if ((Get-Address $ArgosyWindow).TrimEnd('/') -ne 'https://app.meetkevin.com/data/alpha') { throw 'Alpha URL changed during save' }
if (-not [ArgosyAlphaNative]::Save([IntPtr]$ArgosyDialog.Current.NativeWindowHandle, $OutputPath)) {
    $cancel = Get-Named $ArgosyDialog 'Cancel'
    if ($null -ne $cancel) { Invoke-Control $cancel }
    throw 'Could not verify Webpage Complete format and automatic filename; save cancelled'
}
$ArgosySaveDeadline = [DateTime]::UtcNow.AddSeconds(35)
$ArgosyStable = 0
$ArgosyLastSize = -1
while ([DateTime]::UtcNow -lt $ArgosySaveDeadline) {
    Start-Sleep -Milliseconds 500
    if (-not (Test-Path -LiteralPath $OutputPath)) { continue }
    $files = @(Get-Item -LiteralPath $OutputPath)
    $companion = Join-Path (Split-Path -Parent $OutputPath) ([IO.Path]::GetFileNameWithoutExtension($OutputPath) + '_files')
    if (Test-Path -LiteralPath $companion) { $files += @(Get-ChildItem -LiteralPath $companion -File) }
    $size = ($files | Measure-Object -Property Length -Sum).Sum
    if ($size -eq $ArgosyLastSize) { $ArgosyStable++ } else { $ArgosyStable = 0 }
    if ($ArgosyStable -ge 6) {
        @{status='saved'; path=$OutputPath; google_account_selected=$ArgosyAccountClicked; visible_report_headings=$headings.Count} | ConvertTo-Json -Compress
        exit 0
    }
    $ArgosyLastSize = $size
}
throw 'Saved page did not settle; no import was committed'

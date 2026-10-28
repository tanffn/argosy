<# Capture the user's authorized Alpha page in normal Chrome. No cookie export,
   credential reading, keystroke injection, extension, or remote-debugging port. #>
param(
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [Parameter(Mandatory=$true)][string]$GoogleEmail,
    # Explicit operator recovery only: resume the user's supplied Chrome window.
    # Scheduled captures leave this unset and open their own window as before.
    [long]$ExistingWindowHandle = 0
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
  [DllImport("kernel32.dll")] public static extern bool ProcessIdToSessionId(uint p, out uint s);
  [DllImport("wtsapi32.dll")] public static extern bool WTSQuerySessionInformationW(IntPtr server, uint session, int info, out IntPtr data, out uint size);
  [DllImport("wtsapi32.dll")] public static extern void WTSFreeMemory(IntPtr data);
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
    uint session; IntPtr data; uint size;
    if(!ProcessIdToSessionId((uint)System.Diagnostics.Process.GetCurrentProcess().Id, out session)) return false;
    if(!WTSQuerySessionInformationW(IntPtr.Zero, session, 25, out data, out size)) return false;
    try {
      // WTSINFOEXW level-1 union starts at offset 8. Fail closed for unknown,
      // disconnected, or locked sessions; SwitchDesktop is not a lock probe.
      if(data==IntPtr.Zero || size<20 || Marshal.ReadInt32(data,0)!=1 ||
         (uint)Marshal.ReadInt32(data,8)!=session || Marshal.ReadInt32(data,12)!=0 ||
         Marshal.ReadInt32(data,16)!=1) return false;
    } finally { WTSFreeMemory(data); }
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
# One shared grace budget, not 30 seconds per click. Never steal focus.
$script:ArgosyFocusWaitRemainingMs = 30000
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
function Get-PageDocument($Root) {
    try {
        $documents = @($Root.FindAll($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Document))) | Where-Object {
            if ($_.Current.IsOffscreen) { return $false }
            $parent = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($_)
            while ($null -ne $parent) {
                if ($parent.Current.ControlType -eq [System.Windows.Automation.ControlType]::Document) { return $false }
                if ($parent.Current.NativeWindowHandle -eq $Root.Current.NativeWindowHandle) { return $true }
                $parent = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($parent)
            }
            return $false
        })
        if ($documents.Count -gt 1) { throw 'Ambiguous top-level browser documents' }
        if ($documents.Count -eq 1) { return $documents[0] }
    } catch [System.Windows.Automation.ElementNotAvailableException] {
        # Navigation can replace the accessible document between enumeration
        # and inspection. This is a read-only probe: wait for a fresh tree,
        # without retrying any click, login action or save operation.
        return $null
    }
}
function Get-Address($Root) {
    # The omnibox is editable and can contain an unsubmitted URL. Chrome's
    # Document ValuePattern identifies the committed page instead.
    $document = Get-PageDocument $Root
    if ($null -eq $document) { return '' }
    $value = $null
    if (-not $document.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$value)) {
        throw 'Committed document URL is unavailable; refusing browser interaction'
    }
    return $value.Current.Value
}
function Test-OwnedControl($Control) {
    $ancestor = $Control
    while ($null -ne $ancestor) {
        $handle = [IntPtr]$ancestor.Current.NativeWindowHandle
        if ($handle -ne [IntPtr]::Zero -and
            ($handle -eq [IntPtr]$ArgosyWindow.Current.NativeWindowHandle -or
             [ArgosyAlphaNative]::GetAncestor($handle, 3) -eq [IntPtr]$ArgosyWindow.Current.NativeWindowHandle)) { return $true }
        $ancestor = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($ancestor)
    }
    return $false
}
function Assert-PageControl($Control) {
    if (-not [ArgosyAlphaNative]::Unlocked()) { throw '[DESKTOP_INTERRUPTED] Desktop locked during capture' }
    # UIA Invoke targets an element, not the foreground app. Background actions
    # must belong to the captured window's document, never its toolbar or tabs.
    $ancestor = $Control
    $documentCount = 0
    $targetDocument = $null
    while ($null -ne $ancestor) {
        if ($ancestor.Current.ControlType -eq [System.Windows.Automation.ControlType]::Document) {
            $documentCount++
            $targetDocument = $ancestor
        }
        if ($ancestor.Current.NativeWindowHandle -eq $ArgosyWindow.Current.NativeWindowHandle) {
            $loadedDocument = Get-PageDocument $ArgosyWindow
            if ($documentCount -eq 1 -and $null -ne $loadedDocument -and
                ($targetDocument.GetRuntimeId() -join ',') -eq ($loadedDocument.GetRuntimeId() -join ',')) { return }
            break
        }
        $ancestor = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($ancestor)
    }
    throw 'Browser action is not inside the captured page document'
}
function Invoke-Control($Control, [switch]$PageAction) {
    if ($null -eq $Control) { throw 'Expected browser control is absent; page layout may have changed' }
    if ($PageAction) { Assert-PageControl $Control } else {
        Assert-OwnedWindow
        if (-not (Test-OwnedControl $Control)) { throw 'Browser control belongs to another window' }
    }
    $current = [Uri](Get-Address $ArgosyWindow)
    if ($current.Scheme -ne 'https' -or $current.Port -ne 443 -or
        ($current.Host -notin $ArgosySiteHosts -and -not ($ArgosyGoogleClicked -and $current.Host -eq 'accounts.google.com'))) {
        throw 'Browser origin changed before interaction'
    }
    $Control.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}
function Assert-OwnedWindow {
    while ($true) {
        if (-not [ArgosyAlphaNative]::Unlocked()) {
            throw '[DESKTOP_INTERRUPTED] Desktop locked during capture; no further interaction'
        }
        $foreground = [ArgosyAlphaNative]::GetForegroundWindow()
        $owner = [ArgosyAlphaNative]::GetAncestor($foreground, 3)
        if ($foreground -eq [IntPtr]$ArgosyWindow.Current.NativeWindowHandle -or $owner -eq [IntPtr]$ArgosyWindow.Current.NativeWindowHandle) { return }
        if ($script:ArgosyFocusWaitRemainingMs -le 0) {
            throw '[DESKTOP_INTERRUPTED] Another window kept focus; capture stopped without interacting with it'
        }
        Start-Sleep -Milliseconds 500
        $script:ArgosyFocusWaitRemainingMs -= 500
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
if (-not [ArgosyAlphaNative]::Unlocked()) { throw '[DESKTOP_DEFERRED] Desktop is locked; browser was not opened' }
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $OutputPath) { throw 'Capture target already exists; refusing overwrite' }
if (-not (Test-Path -LiteralPath (Split-Path -Parent $OutputPath))) { throw 'Capture output directory is missing' }
$ArgosyBefore = @(Get-ChromeWindows | ForEach-Object { $_.Current.NativeWindowHandle })
$ArgosyChrome = @('C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe') | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $ArgosyChrome) { throw 'Normal Google Chrome is not installed' }
$ArgosyWindow = $null
if ($ExistingWindowHandle -ne 0) {
    $existing = @(Get-ChromeWindows | Where-Object { $_.Current.NativeWindowHandle -eq $ExistingWindowHandle })
    if ($existing.Count -ne 1) { throw 'Supplied Chrome window is absent or ambiguous' }
    $existingAddress = [Uri](Get-Address $existing[0])
    if ($existingAddress.Scheme -ne 'https' -or $existingAddress.Port -ne 443 -or
        $existingAddress.Host -notin $ArgosySiteHosts) {
        throw 'Supplied Chrome window is not on an authorized Meet Kevin origin'
    }
    $ArgosyWindow = $existing[0]
} else {
    # A visible browser is intentional; the caller's PowerShell process is hidden.
    Start-Process -FilePath $ArgosyChrome -ArgumentList '--new-window','https://app.meetkevin.com/data/alpha' | Out-Null
}
$ArgosyDeadline = [DateTime]::UtcNow.AddSeconds(180)
$ArgosyGoogleClicked = $false
$ArgosyAccountClicked = $false
$ArgosyReady = $false
$ArgosyLastStage = 'opening_chrome'
$ArgosyLoginChallenge = $false
$ArgosyNewWindowCount = 0
while ([DateTime]::UtcNow -lt $ArgosyDeadline) {
    Start-Sleep -Milliseconds 600
    if (-not [ArgosyAlphaNative]::Unlocked()) { throw '[DESKTOP_INTERRUPTED] Desktop locked during capture; attempt consumed, no automatic reopening today' }
    if ($null -eq $ArgosyWindow) {
        $newWindows = @(Get-ChromeWindows | Where-Object { $ArgosyBefore -notcontains $_.Current.NativeWindowHandle })
        $ArgosyNewWindowCount = $newWindows.Count
        if ($ArgosyNewWindowCount -gt 0) { $ArgosyLastStage = 'waiting_for_accessible_page' }
        $candidates = @($newWindows | Where-Object {
            (Get-Address $_) -match '^https://(app\.meetkevin\.com|sso\.meet-kevin\.com)/'
        })
        if ($candidates.Count -gt 1) { throw 'Ambiguous newly opened capture windows' }
        if ($candidates.Count -eq 1) { $ArgosyWindow = $candidates[0] }
        if ($null -eq $ArgosyWindow) { continue }
    }
    $loadedAddress = Get-Address $ArgosyWindow
    if (-not $loadedAddress) { continue }
    $ArgosyAddress = [Uri]$loadedAddress
    $ArgosyLastStage = 'site_loading'
    $ArgosyLoginChallenge = $false
    if ($ArgosyAddress.Scheme -ne 'https' -or $ArgosyAddress.Port -ne 443) { throw 'Unexpected insecure browser origin' }
    if ($ArgosyAddress.Host -eq 'accounts.google.com' -and $ArgosyGoogleClicked) {
        $ArgosyLastStage = 'google_account_selection'
        if (-not $ArgosyAccountClicked) {
            # Google renders account choices as links OR buttons. Never select
            # a different account, inspect credentials, or click an ambiguous match.
            $choices = @($ArgosyWindow.FindAll($ArgosyScope,
                [System.Windows.Automation.Condition]::TrueCondition) | Where-Object {
                    $pattern = $null
                    $_.Current.ControlType -in @([System.Windows.Automation.ControlType]::Hyperlink,
                                                [System.Windows.Automation.ControlType]::Button) -and
                    $_.Current.Name -match ('(?<![\w.+-])' + [regex]::Escape($GoogleEmail) + '(?![\w.-])') -and
                    $_.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)
                })
            if ($choices.Count -eq 1) { Invoke-Control $choices[0] -PageAction; $ArgosyAccountClicked = $true }
            if ($choices.Count -gt 1) { throw '[CAPTURE_INCOMPLETE] Ambiguous Google account controls; no account selected' }
        }
        if ($ArgosyAccountClicked) { $ArgosyLastStage = 'google_callback' }
        $password = $ArgosyWindow.FindFirst($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::IsPasswordProperty, $true)))
        $ArgosyLoginChallenge = ($null -ne $password -or
            $ArgosyAddress.AbsolutePath -match '/challenge/|/signin/oauth/consent')
        continue # Password/MFA/CAPTCHA is for the user; never inspect or bypass.
    }
    if ($ArgosyAddress.Host -notin $ArgosySiteHosts) {
        # Host only: OAuth URLs can contain authorization codes and state.
        throw "Browser left the authorized login/site origins (host=$($ArgosyAddress.Host); google_started=$ArgosyGoogleClicked)"
    }
    if ($ArgosyAddress.Host -eq 'sso.meet-kevin.com') {
        $ArgosyLastStage = if ($ArgosyGoogleClicked) { 'returned_to_meetkevin_sign_in' } else { 'meetkevin_sign_in' }
    }
    $ArgosyLogin = Get-Named $ArgosyWindow 'Sign in with Google' -Invokable
    if ($null -eq $ArgosyLogin) { $ArgosyLogin = Get-Named $ArgosyWindow 'Continue with Google' -Invokable }
    if ($null -ne $ArgosyLogin -and -not $ArgosyGoogleClicked) {
        $ArgosyLastStage = 'starting_google'
        Invoke-Control $ArgosyLogin -PageAction; $ArgosyGoogleClicked = $true; continue
    }
    if ($ArgosyAddress.AbsolutePath.TrimEnd('/') -eq '/data/alpha') {
        $ArgosyLastStage = 'waiting_for_report'
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
        if ($null -ne $alpha) { Invoke-Control $alpha -PageAction; continue }
        $data = Get-Named $ArgosyWindow 'DATA' -Invokable
        if ($null -ne $data) { Invoke-Control $data -PageAction; continue }
    }
}
if (-not $ArgosyReady) {
    if ($ArgosyLoginChallenge) { throw '[LOGIN_REQUIRED] Google requires interactive verification in Chrome; no capture imported' }
    # A loading/selector/OAuth timeout does NOT prove credentials are missing.
    throw "[CAPTURE_INCOMPLETE] Alpha automation did not finish (stage=$ArgosyLastStage; new_windows=$ArgosyNewWindowCount; google_started=$ArgosyGoogleClicked; account_selected=$ArgosyAccountClicked). No login challenge was verified; no capture imported"
}
if ((Get-Address $ArgosyWindow).TrimEnd('/') -ne 'https://app.meetkevin.com/data/alpha') { throw 'Alpha URL changed before save' }
$ArgosyMenu = $ArgosyWindow.FindFirst($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'view_1007')))
Assert-Alpha
$ArgosyMenu.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Start-Sleep -Milliseconds 300
$ArgosyMenus = $ArgosyDesktop.FindAll($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::MenuItem)))
$ArgosyShares = @($ArgosyMenus | Where-Object { $_.Current.Name -eq 'Cast, save, and share' -and (Test-OwnedControl $_) })
if ($ArgosyShares.Count -ne 1) { throw 'Owned Chrome share menu is absent or ambiguous' }
$ArgosyShare = $ArgosyShares[0]
Assert-Alpha
$ArgosyShare.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Start-Sleep -Milliseconds 300
$ArgosySaves = @($ArgosyDesktop.FindAll($ArgosyScope, (New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::MenuItem))) |
    Where-Object { $_.Current.Name -match '^Save page as' -and (Test-OwnedControl $_) })
if ($ArgosySaves.Count -ne 1) { throw 'Owned Chrome save menu is absent or ambiguous' }
$ArgosySave = $ArgosySaves[0]
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

using System.Globalization;
using BedrockCapeSync.Resources;

namespace BedrockCapeSync;

public class MainForm : Form
{
    private CancellationTokenSource? _cts;
    private string _currentVerificationUrl = "";
    private SyncResult? _lastResult;

    // Language button
    private readonly Button _btnLang = new();

    // Panels
    private readonly Panel _panelStart = new();
    private readonly Panel _panelDeviceCode = new();
    private readonly Panel _panelProgress = new();
    private readonly Panel _panelResult = new();
    private readonly Panel _panelError = new();

    // Start controls
    private readonly Button _btnStartLogin = new();

    // Device code controls
    private readonly Label _lblCodePrompt = new();
    private readonly Label _lblUserCode = new();
    private readonly Label _lblUrl = new();
    private readonly Button _btnCopyLink = new();
    private readonly Label _lblHint = new();
    private readonly Label _lblWaitingStatus = new();

    // Progress controls
    private readonly Label _lblProgressStep = new();
    private readonly ProgressBar _progressBar = new();

    // Result controls
    private readonly Label _lblResultBefore = new();
    private readonly Label _lblResultAfter = new();
    private readonly Label _lblCapesHeader = new();
    private readonly ListBox _lstCapes = new();
    private readonly Button _btnSwitchAccount = new();

    // Error controls
    private readonly Label _lblErrorTitle = new();
    private readonly TextBox _txtErrorDetails = new();
    private readonly Button _btnRestart = new();

    public MainForm()
    {
        InitializeComponents();
        ApplyLocalization();
        ShowPanel(_panelStart);
    }

    private void InitializeComponents()
    {
        ClientSize = new Size(500, 480);
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Segoe UI", 9F, FontStyle.Regular, GraphicsUnit.Point);

        // Language toggle button (top right, floating above panels)
        _btnLang.Size = new Size(52, 28);
        _btnLang.Location = new Point(ClientSize.Width - 65, 10);
        _btnLang.Font = new Font("Segoe UI", 9F, FontStyle.Bold, GraphicsUnit.Point);
        _btnLang.Click += (_, _) => ToggleLanguage();
        Controls.Add(_btnLang);

        // 1. Panel Start
        _panelStart.Dock = DockStyle.Fill;
        _btnStartLogin.Font = new Font("Segoe UI", 11F, FontStyle.Regular, GraphicsUnit.Point);
        _btnStartLogin.Size = new Size(240, 48);
        _btnStartLogin.Location = new Point((ClientSize.Width - 240) / 2, 195);
        _btnStartLogin.Click += async (_, _) => await StartLoginProcessAsync();
        _panelStart.Controls.Add(_btnStartLogin);

        // 2. Panel Device Code
        _panelDeviceCode.Dock = DockStyle.Fill;

        _lblCodePrompt.Location = new Point(35, 30);
        _lblCodePrompt.AutoSize = true;
        _lblCodePrompt.Font = new Font("Segoe UI", 10F, FontStyle.Regular, GraphicsUnit.Point);

        _lblUserCode.Location = new Point(35, 55);
        _lblUserCode.AutoSize = true;
        _lblUserCode.Font = new Font("Segoe UI", 26F, FontStyle.Bold, GraphicsUnit.Point);

        _lblUrl.Location = new Point(35, 125);
        _lblUrl.Size = new Size(430, 40);
        _lblUrl.Font = new Font("Segoe UI", 9.5F, FontStyle.Underline, GraphicsUnit.Point);
        _lblUrl.ForeColor = SystemColors.HotTrack;

        _btnCopyLink.Size = new Size(180, 36);
        _btnCopyLink.Location = new Point(35, 175);
        _btnCopyLink.Click += (_, _) =>
        {
            if (!string.IsNullOrEmpty(_currentVerificationUrl))
            {
                try
                {
                    Clipboard.SetText(_currentVerificationUrl);
                }
                catch
                {
                    // Игнорируем временную блокировку буфера обмена ОС
                }
            }
        };

        _lblHint.Location = new Point(35, 230);
        _lblHint.Size = new Size(430, 45);
        _lblHint.Font = new Font("Segoe UI", 9.5F, FontStyle.Regular, GraphicsUnit.Point);

        _lblWaitingStatus.Location = new Point(35, 290);
        _lblWaitingStatus.AutoSize = true;
        _lblWaitingStatus.Font = new Font("Segoe UI", 10F, FontStyle.Italic, GraphicsUnit.Point);

        _panelDeviceCode.Controls.AddRange(
        [
            _lblCodePrompt,
            _lblUserCode,
            _lblUrl,
            _btnCopyLink,
            _lblHint,
            _lblWaitingStatus
        ]);

        // 3. Panel Progress
        _panelProgress.Dock = DockStyle.Fill;

        _lblProgressStep.Location = new Point(35, 180);
        _lblProgressStep.Size = new Size(430, 30);
        _lblProgressStep.TextAlign = ContentAlignment.MiddleCenter;
        _lblProgressStep.Font = new Font("Segoe UI", 12F, FontStyle.Bold, GraphicsUnit.Point);

        _progressBar.Location = new Point(50, 225);
        _progressBar.Size = new Size(400, 24);
        _progressBar.Style = ProgressBarStyle.Marquee;
        _progressBar.MarqueeAnimationSpeed = 30;

        _panelProgress.Controls.AddRange([_lblProgressStep, _progressBar]);

        // 4. Panel Result
        _panelResult.Dock = DockStyle.Fill;

        _lblResultBefore.Location = new Point(30, 20);
        _lblResultBefore.AutoSize = true;
        _lblResultBefore.Font = new Font("Segoe UI", 10F, FontStyle.Regular, GraphicsUnit.Point);

        _lblResultAfter.Location = new Point(30, 45);
        _lblResultAfter.AutoSize = true;
        _lblResultAfter.Font = new Font("Segoe UI", 10F, FontStyle.Bold, GraphicsUnit.Point);

        _lblCapesHeader.Location = new Point(30, 75);
        _lblCapesHeader.AutoSize = true;
        _lblCapesHeader.Font = new Font("Segoe UI", 10F, FontStyle.Bold, GraphicsUnit.Point);

        _lstCapes.Location = new Point(30, 100);
        _lstCapes.Size = new Size(440, 295);
        _lstCapes.Font = new Font("Segoe UI", 9.5F, FontStyle.Regular, GraphicsUnit.Point);
        _lstCapes.SelectionMode = SelectionMode.None;
        _lstCapes.DrawMode = DrawMode.OwnerDrawFixed;
        _lstCapes.ItemHeight = 22;
        _lstCapes.DrawItem += LstCapes_DrawItem;

        _btnSwitchAccount.Size = new Size(220, 36);
        _btnSwitchAccount.Location = new Point(30, 410);
        _btnSwitchAccount.Click += (_, _) =>
        {
            CancelCurrentOperation();
            ShowPanel(_panelStart);
        };

        _panelResult.Controls.AddRange(
        [
            _lblResultBefore,
            _lblResultAfter,
            _lblCapesHeader,
            _lstCapes,
            _btnSwitchAccount
        ]);

        // 5. Panel Error
        _panelError.Dock = DockStyle.Fill;

        _lblErrorTitle.Location = new Point(30, 25);
        _lblErrorTitle.AutoSize = true;
        _lblErrorTitle.Font = new Font("Segoe UI", 12F, FontStyle.Bold, GraphicsUnit.Point);
        _lblErrorTitle.ForeColor = Color.Red;

        _txtErrorDetails.Location = new Point(30, 60);
        _txtErrorDetails.Size = new Size(440, 330);
        _txtErrorDetails.Multiline = true;
        _txtErrorDetails.ReadOnly = true;
        _txtErrorDetails.ScrollBars = ScrollBars.Vertical;
        _txtErrorDetails.ForeColor = Color.Red;
        _txtErrorDetails.BackColor = SystemColors.Window;
        _txtErrorDetails.Font = new Font("Segoe UI", 9.5F, FontStyle.Regular, GraphicsUnit.Point);

        _btnRestart.Size = new Size(180, 36);
        _btnRestart.Location = new Point(30, 405);
        _btnRestart.Click += (_, _) =>
        {
            CancelCurrentOperation();
            ShowPanel(_panelStart);
        };

        _panelError.Controls.AddRange([_lblErrorTitle, _txtErrorDetails, _btnRestart]);

        // Add all panels to form
        Controls.AddRange([_panelStart, _panelDeviceCode, _panelProgress, _panelResult, _panelError]);
    }

    private void LstCapes_DrawItem(object? sender, DrawItemEventArgs e)
    {
        if (e.Index < 0 || e.Index >= _lstCapes.Items.Count)
        {
            return;
        }

        e.DrawBackground();

        string text = _lstCapes.Items[e.Index].ToString() ?? "";
        bool isNew = text.StartsWith("[NEW] ", StringComparison.OrdinalIgnoreCase);

        int x = e.Bounds.X + 4;
        int y = e.Bounds.Y + 2;

        if (isNew)
        {
            using var greenBrush = new SolidBrush(Color.ForestGreen);
            using var boldFont = new Font(_lstCapes.Font, FontStyle.Bold);
            e.Graphics.DrawString("[NEW] ", boldFont, greenBrush, x, y);

            int tagWidth = TextRenderer.MeasureText("[NEW] ", boldFont).Width - 4;
            string title = text[6..];
            using var textBrush = new SolidBrush(e.ForeColor);
            e.Graphics.DrawString(title, _lstCapes.Font, textBrush, x + tagWidth, y);
        }
        else
        {
            using var textBrush = new SolidBrush(e.ForeColor);
            e.Graphics.DrawString(text, _lstCapes.Font, textBrush, x, y);
        }

        e.DrawFocusRectangle();
    }

    private void ToggleLanguage()
    {
        bool isRu = Strings.CurrentCulture.TwoLetterISOLanguageName.Equals("ru", StringComparison.OrdinalIgnoreCase);
        Strings.CurrentCulture = new CultureInfo(isRu ? "en" : "ru");
        ApplyLocalization();
    }

    private void ApplyLocalization()
    {
        Text = Strings.Get("AppTitle");
        bool isRu = Strings.CurrentCulture.TwoLetterISOLanguageName.Equals("ru", StringComparison.OrdinalIgnoreCase);
        _btnLang.Text = isRu ? "EN" : "RU";

        _btnStartLogin.Text = Strings.Get("BtnLogin");
        _lblCodePrompt.Text = Strings.Get("CodePrompt");
        _btnCopyLink.Text = Strings.Get("BtnCopyLink");
        _lblHint.Text = Strings.Get("HintDeviceCode");
        _lblWaitingStatus.Text = Strings.Get("StatusWaiting");
        _btnSwitchAccount.Text = Strings.Get("BtnSwitchAccount");
        _lblErrorTitle.Text = Strings.Get("ErrorTitle");
        _btnRestart.Text = Strings.Get("BtnRestart");

        if (_lastResult != null)
        {
            UpdateResultLabels(_lastResult);
        }
    }

    private void ShowPanel(Panel panel)
    {
        if (InvokeRequired)
        {
            Invoke(() => ShowPanel(panel));
            return;
        }

        _panelStart.Visible = panel == _panelStart;
        _panelDeviceCode.Visible = panel == _panelDeviceCode;
        _panelProgress.Visible = panel == _panelProgress;
        _panelResult.Visible = panel == _panelResult;
        _panelError.Visible = panel == _panelError;

        panel.BringToFront();
        _btnLang.BringToFront();
    }

    private void CancelCurrentOperation()
    {
        try
        {
            _cts?.Cancel();
            _cts?.Dispose();
        }
        catch
        {
            // Игнорируем
        }
        finally
        {
            _cts = null;
        }
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        base.OnFormClosing(e);
        CancelCurrentOperation();
    }

    private async Task StartLoginProcessAsync()
    {
        CancelCurrentOperation();
        _cts = new CancellationTokenSource();
        var ct = _cts.Token;

        try
        {
            var deviceCode = await BedrockSync.StartDeviceCodeLoginAsync(ct);

            _currentVerificationUrl = $"https://www.microsoft.com/link?otc={deviceCode.UserCode}";
            _lblUserCode.Text = deviceCode.UserCode;
            _lblUrl.Text = _currentVerificationUrl;
            ShowPanel(_panelDeviceCode);

            string msaAccessToken = await BedrockSync.PollMsaTokenAsync(
                deviceCode.DeviceCode,
                deviceCode.Interval,
                deviceCode.ExpiresIn,
                ct);

            ShowPanel(_panelProgress);

            var syncResult = await BedrockSync.RunSyncWorkflowAsync(
                msaAccessToken,
                step =>
                {
                    if (InvokeRequired)
                    {
                        Invoke(() => _lblProgressStep.Text = step);
                    }
                    else
                    {
                        _lblProgressStep.Text = step;
                    }
                },
                () =>
                {
                    var dlgResult = MessageBox.Show(
                        this,
                        Strings.Get("ConfirmCreateProfile"),
                        Strings.Get("DialogProfileTitle"),
                        MessageBoxButtons.YesNo,
                        MessageBoxIcon.Question);

                    return Task.FromResult(dlgResult == DialogResult.Yes);
                },
                warning =>
                {
                    if (InvokeRequired)
                    {
                        Invoke(() => MessageBox.Show(this, warning, Strings.Get("WarningTitle"), MessageBoxButtons.OK, MessageBoxIcon.Warning));
                    }
                    else
                    {
                        MessageBox.Show(this, warning, Strings.Get("WarningTitle"), MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    }
                },
                ct);

            _lastResult = syncResult;
            DisplayResult(syncResult);
        }
        catch (OperationCanceledException)
        {
            // Отмена закрытием окна или сменой экрана
            if (!IsDisposed)
            {
                ShowPanel(_panelStart);
            }
        }
        catch (BedrockSyncException ex)
        {
            DisplayError(ex.ToDisplayString());
        }
        catch (Exception ex)
        {
            DisplayError($"{Strings.Format("LabelError", ex.Message)}");
        }
    }

    private void UpdateResultLabels(SyncResult result)
    {
        int diff = result.CountAfter - result.CountBefore;
        string diffStr = diff >= 0 ? $"+{diff}" : diff.ToString();

        _lblResultBefore.Text = Strings.Format("ResultBefore", result.CountBefore);
        _lblResultAfter.Text = Strings.Format("ResultAfter", result.CountAfter, diffStr);
        _lblCapesHeader.Text = Strings.Format("CapesHeader", result.Capes.Count);
    }

    private void DisplayResult(SyncResult result)
    {
        if (InvokeRequired)
        {
            Invoke(() => DisplayResult(result));
            return;
        }

        UpdateResultLabels(result);

        _lstCapes.Items.Clear();
        foreach (var cape in result.Capes)
        {
            string itemText = cape.IsNew ? $"[NEW] {cape.Title}" : cape.Title;
            _lstCapes.Items.Add(itemText);
        }

        ShowPanel(_panelResult);
    }

    private void DisplayError(string message)
    {
        if (InvokeRequired)
        {
            Invoke(() => DisplayError(message));
            return;
        }

        _txtErrorDetails.Text = message;
        ShowPanel(_panelError);
    }
}

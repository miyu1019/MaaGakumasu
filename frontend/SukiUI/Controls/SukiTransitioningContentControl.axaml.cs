using System;
using System.Collections.Generic;
using System.Threading;
using Avalonia;
using Avalonia.Animation;
using Avalonia.Controls;
using Avalonia.Controls.Presenters;
using Avalonia.Controls.Primitives;
using Avalonia.Interactivity;
using Avalonia.Media;
using Avalonia.Styling;
using Avalonia.Threading;

namespace SukiUI.Controls
{
    // TODO: This needs fairly significant work to make a bit more bomb proof
    // There are probably some more gains that can be made in terms of performance.
    // Unfortunately we're still bound by the arrange of controls having to happen on the main thread.
    public class SukiTransitioningContentControl : TemplatedControl
    {
        internal static readonly StyledProperty<object?> FirstBufferProperty =
            AvaloniaProperty.Register<SukiTransitioningContentControl, object?>(nameof(FirstBuffer));

        internal object? FirstBuffer
        {
            get => GetValue(FirstBufferProperty);
            set => SetValue(FirstBufferProperty, value);
        }

        internal static readonly StyledProperty<object?> SecondBufferProperty =
            AvaloniaProperty.Register<SukiTransitioningContentControl, object?>(nameof(SecondBuffer));

        internal object? SecondBuffer
        {
            get => GetValue(SecondBufferProperty);
            set => SetValue(SecondBufferProperty, value);
        }

        public static readonly StyledProperty<object?> ContentProperty = AvaloniaProperty.Register<SukiTransitioningContentControl, object?>(nameof(Content));

        public object? Content
        {
            get => GetValue(ContentProperty);
            set => SetValue(ContentProperty, value);
        }

        private ContentPresenter? _firstBuffer = null;
        private ContentPresenter? _secondBuffer = null;
        private Panel? _root = null;
        private ContentPresenter? _active = null;

        // 常驻内容：切走时只隐藏，不移出可视树，切回时免去重建样式与重新排版。
        private readonly Dictionary<object, ContentPresenter> _kept = new();
        private readonly HashSet<object> _keepRequested = new();

        /// <summary>设置某内容是否常驻；关闭时若它正隐藏则立即释放，正显示则在切走后释放。</summary>
        public void SetKeepAlive(object content, bool keepAlive)
        {
            if (keepAlive)
            {
                _keepRequested.Add(content);
                return;
            }
            _keepRequested.Remove(content);
            if (_kept.TryGetValue(content, out var presenter) && !ReferenceEquals(presenter, _active))
                ReleaseKept(content);
        }

        private void ReleaseKept(object content)
        {
            if (!_kept.Remove(content, out var presenter)) return;
            presenter.Content = null;
            _root?.Children.Remove(presenter);
        }

        private static readonly Animation FadeIn;
        private static readonly Animation FadeOut;

        private object? _contentBeforeApplied;

        static SukiTransitioningContentControl()
        {
            FadeIn = new Animation
            {
                Duration = TimeSpan.FromMilliseconds(400),
                Children =
                {
                    new KeyFrame()
                    {
                        Setters =
                        {
                            new Setter
                            {
                                Property = OpacityProperty,
                                Value = 0d
                            }
                        },
                        Cue = new Cue(0d)
                    },
                    new KeyFrame()
                    {
                        Setters =
                        {
                            new Setter
                            {
                                Property = OpacityProperty,
                                Value = 1d
                            }
                        },
                        Cue = new Cue(1d)
                    }
                },
                FillMode = FillMode.Forward
            };
            FadeOut = new Animation
            {
                Duration = TimeSpan.FromMilliseconds(400),
                Children =
                {
                    new KeyFrame()
                    {
                        Setters =
                        {
                            new Setter
                            {
                                Property = OpacityProperty,
                                Value = 1d
                            }
                        },
                        Cue = new Cue(0d)
                    },
                    new KeyFrame()
                    {
                        Setters =
                        {
                            new Setter
                            {
                                Property = OpacityProperty,
                                Value = 0d
                            }
                        },
                        Cue = new Cue(1d)
                    }
                },
                FillMode = FillMode.Forward
            };
            FadeIn.Duration = FadeOut.Duration = TimeSpan.FromMilliseconds(250);
        }

        private CancellationTokenSource _animCancellationToken = new();
        

        protected override void OnPropertyChanged(AvaloniaPropertyChangedEventArgs change)
        {
            base.OnPropertyChanged(change);
            if(change.Property == ContentProperty)
                PushContent(change.NewValue);
        }

        protected override void OnApplyTemplate(TemplateAppliedEventArgs e)
        {
            base.OnApplyTemplate(e);
            if (e.NameScope.Get<ContentPresenter>("PART_FirstBufferControl") is { } fBuff)
                _firstBuffer = fBuff;
            if (e.NameScope.Get<ContentPresenter>("PART_SecondBufferControl") is { } sBuff)
                _secondBuffer = sBuff;
            _root = e.NameScope.Find<Panel>("PART_Root");
            if (_contentBeforeApplied != null)
            {
                PushContent(_contentBeforeApplied);
                _contentBeforeApplied = null;
            }
        }

        private void PushContent(object? content)
        {
            if (_firstBuffer is null || _secondBuffer is null)
            {
                _contentBeforeApplied = content;
                return;
            }

            try
            {
                _animCancellationToken.Cancel();
                _animCancellationToken.Dispose();
            }
            catch (Exception ex)
            {
                Console.WriteLine(ex.Message);
            }

            _animCancellationToken = new CancellationTokenSource();

            ContentPresenter to;
            if (content != null && _root != null && _keepRequested.Contains(content))
            {
                if (!_kept.TryGetValue(content, out to!))
                {
                    to = new ContentPresenter { Content = content, IsHitTestVisible = false, Opacity = 0 };
                    _kept[content] = to;
                    _root.Children.Insert(0, to);
                }
                to.IsVisible = true;
            }
            else
            {
                if (content != null && _kept.ContainsKey(content))
                {
                    if (_kept.TryGetValue(content, out var stale) && ReferenceEquals(stale, _active)) _active = null;
                    ReleaseKept(content);
                }
                to = ReferenceEquals(_active, _firstBuffer) ? _secondBuffer : _firstBuffer;
                if (ReferenceEquals(to, _firstBuffer)) FirstBuffer = content;
                else SecondBuffer = content;
            }

            var from = _active;
            _active = to;
            if (ReferenceEquals(from, to)) return;
            var fromContent = from?.Content;
            try
            {
                if (from != null)
                {
                    FadeOut.RunAsync(from, _animCancellationToken.Token).ContinueWith(_ =>
                    {
                        Dispatcher.UIThread.Invoke(() =>
                        {
                            if (ReferenceEquals(_active, from)) return;
                            from.IsHitTestVisible = false;
                            if (fromContent != null && _kept.TryGetValue(fromContent, out var kept) && ReferenceEquals(kept, from))
                            {
                                from.IsVisible = false;
                                if (!_keepRequested.Contains(fromContent)) ReleaseKept(fromContent);
                            }
                            else if (ReferenceEquals(from, _firstBuffer) && ReferenceEquals(FirstBuffer, fromContent)) FirstBuffer = null;
                            else if (ReferenceEquals(from, _secondBuffer) && ReferenceEquals(SecondBuffer, fromContent)) SecondBuffer = null;
                        });
                    });
                }
                FadeIn.RunAsync(to, _animCancellationToken.Token).ContinueWith(_ =>
                    Dispatcher.UIThread.Invoke(() =>
                    {
                        if (ReferenceEquals(_active, to)) to.IsHitTestVisible = true;
                    }));
            }
            catch
            {
                // ignored
            }
        }

        protected override void OnUnloaded(RoutedEventArgs e)
        {
            base.OnUnloaded(e);
            _animCancellationToken.Dispose();
        }
    }
}
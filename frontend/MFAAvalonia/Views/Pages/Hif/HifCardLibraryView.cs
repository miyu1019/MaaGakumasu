using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using MFAAvalonia.Helper;
using System;
using System.Collections.Generic;
using System.Linq;

namespace MFAAvalonia.Views.Pages.Hif;

/// <summary>「卡库」：按职业筛选全部卡牌，点卡加入或移出出牌优先级、获取与定制、授业换出卡。</summary>
public sealed class HifCardLibraryView : UserControl
{
    private static readonly (HifLibraryTarget Target, string Label)[] Targets =
    [
        (HifLibraryTarget.Priority, "出牌优先级"),
        (HifLibraryTarget.Acquisition, "获取与定制"),
        (HifLibraryTarget.SwapOut, "授业换出卡")
    ];

    private readonly HifPriorityPanel _cards;
    private readonly HifCustomCardPanel _custom;
    private readonly Func<string> _profession;
    private readonly HifSegmented _target = new(Targets.Select(item => item.Label));
    private readonly HifCardPicker _picker;
    private readonly IReadOnlyList<HifLibraryCard> _all;

    public HifLibraryTarget Target { get; private set; } = HifLibraryTarget.Priority;

    public const string Legend = "角标：出 = 出牌优先级 · 获 = 已开优先获取 · 定 = 定制名单 · 换 = 授业换出卡 · 蓝框 = 已在所选名单";

    /// <summary>当前筛选结果。</summary>
    public IReadOnlyList<HifLibraryCard> Visible => _picker.Visible;

    public static string HelpText =>
        "点卡加入\n先选要加入的名单：出牌优先级、获取与定制或授业换出卡。点卡加入，再点一次移出。出牌编排的起始卡与后续卡在「出牌编排」标签中选择。\n\n" +
        "获取与定制\n加入后在「获取与定制」标签设置「优先获取」开关和定制次数。新加入的卡「优先获取」默认关闭；没有定制项目的卡只用于优先获取，加入后直接开启。再点一次同时移出优先获取和定制。\n\n" +
        "默认筛选\n只显示当前职业的计划与不限计划：好调、集中为感性，好印象、元気为理性，全力、強気为非凡。关闭开关可查看全部卡牌。\n\n" +
        "排序\n同计划另一职业已在用的卡排在最前，其次按稀有度从高到低、A 卡在前。\n\n" +
        "标记\n蓝框表示这张卡已在上方所选的名单中。卡图右上角的角标与所选名单无关，标出这张卡已在当前职业的哪些名单：出（出牌优先级）、获（获取与定制中已开优先获取）、定（获取与定制中的定制名单）、换（授业换出卡）。悬停可看稀有度、计划、类型、来源和已在的名单。\n\n" +
        "保存\n卡库直接修改各标签的编辑数据，点页面顶部「保存」写入配置。";

    public HifCardLibraryView(HifPriorityPanel cards, HifCustomCardPanel custom, Func<string> profession)
    {
        _cards = cards;
        _custom = custom;
        _profession = profession;
        _all = cards.LibraryCards();
        _picker = new HifCardPicker(_all, profession)
        {
            Allowed = card => _cards.CanAdd(Target, card),
            Preferred = card => Targets.Any(item => Contains(card, item.Target, Sibling(_profession()))),
            Describe = DescribeTile,
            Clicked = Toggle,
            Failure = () => _cards.Status,
            CountText = (visible, allowed) =>
            {
                var selected = _all.Where(_picker!.Allowed).Count(card => Contains(card, Target));
                return $"显示 {visible} / {allowed} 张 · {_profession()}「{Targets.First(item => item.Target == Target).Label}」已有 {selected} 张 · 再点一次可移出";
            }
        };

        var root = new Grid { RowDefinitions = new RowDefinitions("Auto,*"), RowSpacing = 10 };
        var targetRow = new WrapPanel { Orientation = Orientation.Horizontal };
        targetRow.Children.Add(new TextBlock { Text = "点卡加入", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 6) });
        _target.Margin = new Thickness(0, 0, 14, 6);
        targetRow.Children.Add(_target);
        // 角标图例：卡图右上角的字表示这张卡已在哪些名单，与当前选中的名单无关。
        targetRow.Children.Add(new TextBlock
        {
            Text = Legend, Tag = "legend", FontSize = 12, Opacity = HifTheme.SecondaryOpacity, TextWrapping = Avalonia.Media.TextWrapping.Wrap,
            VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 0, 6)
        });
        root.Children.Add(targetRow);
        Grid.SetRow(_picker, 1);
        root.Children.Add(_picker);
        Content = root;

        _target.Select(0);
        _target.SelectionRequested += index => Open(Targets[index].Target);
        Refresh();
    }

    /// <summary>切换要加入的名单，并刷新可选卡牌。</summary>
    public void Open(HifLibraryTarget target)
    {
        Target = target;
        _target.Select(Array.FindIndex(Targets, item => item.Target == target));
        Refresh();
    }

    public void Refresh() => _picker.Refresh();

    /// <summary>与当前职业同计划的另一个职业。</summary>
    private static string Sibling(string profession) => profession switch
    {
        "好调" => "集中", "集中" => "好调", "好印象" => "元気", "元気" => "好印象", "全力" => "強気", _ => "全力"
    };

    /// <summary>获取与定制：在优先获取名单或定制名单中都算已加入。</summary>
    private bool Contains(HifLibraryCard card, HifLibraryTarget target, string? profession = null) =>
        _cards.InList(target, card.Name, profession) || target == HifLibraryTarget.Acquisition && _custom.InList(card.Id, profession);

    private HifCardPicker.TileState DescribeTile(HifLibraryCard card)
    {
        var inTarget = Contains(card, Target);
        var marks = new (bool On, string Badge, string Label)[]
        {
            (_cards.InList(HifLibraryTarget.Priority, card.Name), "出", "出牌优先级"),
            (_cards.InList(HifLibraryTarget.Acquisition, card.Name), "获", "获取与定制 · 已开优先获取"),
            (_custom.InList(card.Id), "定", "获取与定制 · 定制名单"),
            (_cards.InList(HifLibraryTarget.SwapOut, card.Name), "换", "授业换出卡")
        }.Where(mark => mark.On).ToList();
        var detail = HifCardPicker.CardDetail(card);
        if (marks.Count > 0) detail += "\n已在：" + string.Join("、", marks.Select(mark => mark.Label));
        if (Target == HifLibraryTarget.Acquisition && !inTarget)
            detail += _custom.Customizable(card.Id) ? "\n加入后「优先获取」默认关闭" : "\n没有定制项目，加入后只用于优先获取";
        detail += inTarget ? "\n点击移出" : "\n点击加入";
        return new HifCardPicker.TileState(inTarget, false, string.Join(" ", marks.Select(mark => mark.Badge)), detail);
    }

    /// <summary>
    /// 点卡：不在当前名单就加入，已在就移出。获取与定制：可定制的卡加入定制名单（优先获取默认关闭），
    /// 没有定制项目的卡直接加入优先获取；移出时两份名单一起移出。
    /// </summary>
    public bool Toggle(HifLibraryCard card)
    {
        var include = !Contains(card, Target);
        if (Target != HifLibraryTarget.Acquisition) return _cards.SetInList(Target, card.Name, include);
        if (include && _custom.Customizable(card.Id))
        {
            _custom.SetInList(card.Id, true);
            return true;
        }
        _custom.SetInList(card.Id, false);
        return _cards.SetInList(HifLibraryTarget.Acquisition, card.Name, include);
    }
}

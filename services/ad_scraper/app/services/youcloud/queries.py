"""GraphQL documents sent to the YouCloud/AppGrowing endpoint.

One place for the query text so a schema change is a one-file edit.

Two deliberate differences from the query the web app sends:

1. **`__typename` on every `campaign` fragment.** The web app omits it and
   leaves the client to guess an entity's kind from the payload shape
   (`types`+`developer` → AppBrand, `type=401` → Website, `type=400` →
   Playlet). That heuristic is undocumented and breaks silently when the
   platform adds a union member. `__typename` is authoritative and free.

2. **Trimmed selection set.** The web app requests fields that drive UI
   affordances we don't persist. Everything we do persist is here; the
   full response still lands in `ad_materials.raw`, so adding a column
   later is a backfill from JSONB rather than a re-scrape.

The variable list is kept identical to the web app's so any filter an
operator can build in the UI can be pasted into a job's `filters` object
verbatim.
"""

MATERIAL_LIST_OPERATION = "materialList"

MATERIAL_LIST_QUERY = """
query materialList(
    $materialIds: [String]
    $purpose: Int!
    $startDate: LocalDate
    $endDate: LocalDate
    $isAllDate: Int
    $category: [Int]
    $resourceElement: [Int]
    $appStyle: [Int]
    $media: [Int]
    $area: [String]
    $format: [Int]
    $platform: [Int]
    $creativeType: [Int]
    $isNew: Int
    $isNewAd: Int
    $hasPpid: Int
    $resolution: Int
    $field: String
    $keyword: String
    $order: MaterialListSort!
    $page: Int
    $resourceId: String
    $campaign: String
    $appBrand: String
    $lines_digests: MixID
    $isCreative: Int
    $gender: [Int]
    $ageRange: [String]
    $appCashWay: [AppCashWay]
    $materialRatio: [String]
    $isW2a: Int
    $excludeW2a: Int
    $campaignTypeCombine: Int
    $campaignType: [Int]
    $platformCampaignType: [Int]
    $language: [String]
    $videoTime: Int
    $minDuration: NumStr
    $maxDuration: NumStr
    $accurateSearch: Int
    $blockIds: [String]
    $blockAppIds: [String]
    $isPre: Int
    $asr: Int
    $asrLanguage: String
    $playletId: String
    $brandType: BrandType
    $searchDsl: [SearchDsl]
    $isViolation: Int
    $videoShotId: String
    $topLimit: Int
    $hasCooperate: Int
    $cooperateId: NumID
    $faceId: MixID
    $marketingWord: String
    $relMarketingWord: [String]
    $singleArea: Int
    $postpage: Int
    $ppid: String
) {
    materialList(
        materialIds: $materialIds
        purpose: $purpose
        startDate: $startDate
        endDate: $endDate
        isAllDate: $isAllDate
        category: $category
        resourceElement: $resourceElement
        appStyle: $appStyle
        media: $media
        area: $area
        format: $format
        platform: $platform
        creativeType: $creativeType
        isNew: $isNew
        isNewAd: $isNewAd
        hasPpid: $hasPpid
        resolution: $resolution
        field: $field
        keyword: $keyword
        order: $order
        page: $page
        campaign: $campaign
        resourceId: $resourceId
        appBrand: $appBrand
        lines_digests: $lines_digests
        isCreative: $isCreative
        gender: $gender
        ageRange: $ageRange
        appCashWay: $appCashWay
        materialRatio: $materialRatio
        isW2a: $isW2a
        excludeW2a: $excludeW2a
        campaignTypeCombine: $campaignTypeCombine
        campaignType: $campaignType
        platformCampaignType: $platformCampaignType
        language: $language
        videoTime: $videoTime
        minDuration: $minDuration
        maxDuration: $maxDuration
        accurateSearch: $accurateSearch
        blockIds: $blockIds
        blockAppIds: $blockAppIds
        isPre: $isPre
        asr: $asr
        asrLanguage: $asrLanguage
        playletId: $playletId
        brandType: $brandType
        searchDsl: $searchDsl
        isViolation: $isViolation
        videoShotId: $videoShotId
        topLimit: $topLimit
        hasCooperate: $hasCooperate
        cooperateId: $cooperateId
        faceId: $faceId
        marketingWord: $marketingWord
        relMarketingWord: $relMarketingWord
        singleArea: $singleArea
        postpage: $postpage
        ppid: $ppid
    ) {
        page
        total
        limit
        data {
            material {
                id
                type
                startDate
                endDate
                duration
                cnt_ad_id
                similar_cnt
                impression_inc_2y
                violation
                gender
                asr
                media { id name icon description }
                channel { id name icon }
                area { cc name icon }
                format { id name }
                platform { id name }
                resourceElement { id parentId name }
                creative {
                    id
                    type
                    slogan
                    description
                    txtUrl
                    resource { id format width height duration path poster }
                }
                campaign {
                    __typename
                    ... on App {
                        id
                        name
                        icon
                        type
                        minis_type
                        alias
                        developer { id name area { cc name } }
                    }
                    ... on AppBrand {
                        id
                        name
                        icon
                        types
                        alias
                        gp_app_url
                        ios_app_url
                        developer { id name area { cc name } }
                    }
                    ... on Website { id type name icon }
                    ... on Playlet { id type name }
                    ... on Novel { id type name }
                }
            }
        }
    }
}
"""
